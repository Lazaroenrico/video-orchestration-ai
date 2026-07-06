"""Cobertura dos ramos de borda em nodes/stages.py e adapters/base.py.

Foco nos caminhos que os testes de fluxo (graph e2e, reroll) não exercitam:
voice-preview best-effort, perfil de voz inválido, merge de roster sem update,
falhas parciais/totais no node_roster, aprovação sem seleção, e node_drop.
"""
from __future__ import annotations

import pytest

from orchestrator.adapters import base
from orchestrator.adapters.base import VoiceProfile
from orchestrator.graph.state import Item, new_item
from orchestrator.nodes import stages


# ------------------------------------------------------------------ #
# adapters/base.py — VoiceProfile / infer                            #
# ------------------------------------------------------------------ #

def test_voice_profile_rejects_invalid_preset():
    with pytest.raises(ValueError, match="unsupported voice preset"):
        VoiceProfile(preset="banana")  # type: ignore[arg-type]


def test_infer_voice_profile_detects_male_hint():
    profile = base.infer_voice_profile("energetic male skincare creator")
    assert profile is not None
    assert profile.preset == "male"


# ------------------------------------------------------------------ #
# _build_voice_preview — best-effort, nunca quebra offline            #
# ------------------------------------------------------------------ #

async def test_build_voice_preview_none_without_voice_id(tmp_path):
    assert await stages._build_voice_preview(
        object(), {"id": "creator-0"}, run_id="run", media_root=tmp_path
    ) is None


async def test_build_voice_preview_none_for_downloadable_voice_without_source(tmp_path):
    creator = {"id": "creator-0", "voice_id": "https://cdn.example/voice.mp3"}
    assert await stages._build_voice_preview(
        object(), creator, run_id="run", media_root=tmp_path
    ) is None


async def test_build_voice_preview_swallows_synth_errors(tmp_path):
    class _Voice:
        async def synthesize_preview(self, voice_ref):
            raise RuntimeError("síntese indisponível")

    class _Adapter:
        voice = _Voice()

    creator = {"id": "creator-0", "voice_id": "el_opaque_voice_id"}
    result = await stages._build_voice_preview(
        _Adapter(), creator, run_id="run", media_root=tmp_path
    )
    assert result is None


# ------------------------------------------------------------------ #
# _creator_voice_profile                                             #
# ------------------------------------------------------------------ #

def test_creator_voice_profile_none_for_invalid_preset():
    creator = {"voice_profile": {"preset": "invalid", "prompt": ""}}
    assert stages._creator_voice_profile(creator) is None


# ------------------------------------------------------------------ #
# apply_roster_updates                                              #
# ------------------------------------------------------------------ #

def test_apply_roster_updates_keeps_creator_without_matching_update():
    roster = [{"id": "a"}, {"id": "b"}]
    merged = stages.apply_roster_updates(roster, [{"id": "a", "voice_ref": "v"}])
    assert merged[1] == {"id": "b"}  # sem update → preservado intacto
    assert merged[0]["voice_id"] == "v"


# ------------------------------------------------------------------ #
# node_roster — falha parcial vs. total                             #
# ------------------------------------------------------------------ #

def _roster_config(adapter):
    return {
        "configurable": {
            "adapter": adapter,
            "pipeline": {"roster": {"creators": 2}},
            "run": {},
            "thread_id": "run-x",
        }
    }


async def test_node_roster_tolerates_partial_failure():
    class _PartialAdapter:
        async def build_creator(self, *, index, system_prompt, voice_profile):
            if index == 0:
                raise RuntimeError("creator 0 falhou")
            return {"id": f"creator-{index}", "upscaled_base": "mock://img", "voice_id": "voice"}

    result = await stages.node_roster({}, _roster_config(_PartialAdapter()))
    roster = result["roster"]
    assert [c["id"] for c in roster] == ["creator-1"]


async def test_node_roster_raises_when_all_fail():
    class _FailAllAdapter:
        async def build_creator(self, *, index, system_prompt, voice_profile):
            raise RuntimeError(f"creator {index} falhou")

    with pytest.raises(RuntimeError, match="falhou"):
        await stages.node_roster({}, _roster_config(_FailAllAdapter()))


async def test_node_roster_uses_seed_creator_without_building_new_creator():
    seed = {
        "creator_id": "creator-fixed",
        "image_uri": "data:image/png;base64,SEED",
        "voice_ref": "voice-fixed",
        "voice_preview_uri": "data:audio/wav;base64,SEED",
        "angles": ["front", "side"],
    }

    class _BoomAdapter:
        async def build_creator(self, *, index, system_prompt, voice_profile):
            raise AssertionError("build_creator should not be called for seed creator")

    config = {
        "configurable": {
            "adapter": _BoomAdapter(),
            "pipeline": {"roster": {"creators": 2}},
            "run": {"seed_creator": seed},
            "thread_id": "run-seed",
        }
    }

    result = await stages.node_roster({}, config)

    assert len(result["roster"]) == 1
    creator = result["roster"][0]
    assert creator["id"] == "creator-fixed"
    assert creator["upscaled_base"] == "data:image/png;base64,SEED"
    assert creator["image_uri"] == "data:image/png;base64,SEED"
    assert creator["image"] == "data:image/png;base64,SEED"
    assert creator["image_source_uri"] == "data:image/png;base64,SEED"
    assert creator["voice_id"] == "voice-fixed"
    assert creator["voice_ref"] == "voice-fixed"
    assert creator["voice"] == "voice-fixed"
    assert creator["voice_preview_uri"] == "data:audio/wav;base64,SEED"
    assert creator["angles"] == ["front", "side"]


def test_normalize_seed_creator_returns_none_without_id():
    assert stages._normalize_seed_creator({"image_uri": "data:image/png;base64,SEED"}) is None


# ------------------------------------------------------------------ #
# node_approval — aprova todos quando não há seleção explícita        #
# ------------------------------------------------------------------ #

async def test_node_approval_approves_all_when_decision_has_no_selection(monkeypatch):
    monkeypatch.setattr(stages, "interrupt", lambda payload: {})
    config = {"configurable": {"run": {"approve_creators": True}}}
    state = {"roster": [{"id": "creator-0"}, {"id": "creator-1"}]}

    result = await stages.node_approval(state, config)

    assert {c["id"] for c in result["roster"]} == {"creator-0", "creator-1"}


async def test_node_approval_rejects_all_when_selection_empty(monkeypatch):
    monkeypatch.setattr(stages, "interrupt", lambda payload: {"approved": []})
    config = {"configurable": {"run": {"approve_creators": True}}}
    state = {"roster": [{"id": "creator-0"}]}

    result = await stages.node_approval(state, config)

    assert result["roster"] == []


# ------------------------------------------------------------------ #
# _assembly_prompt com run_prompt + node_drop                        #
# ------------------------------------------------------------------ #

def test_assembly_prompt_prepends_run_prompt():
    item = new_item({"id": "concept-0", "hook": "h", "offer": "serum X"})
    prompt = stages._assembly_prompt(item, "Custom operator prompt.", platform="tiktok")
    assert prompt.startswith("Custom operator prompt.")
    assert "Final vertical UGC ad for tiktok." in prompt


def test_video_prompt_prepends_run_prompt():
    item = new_item({"id": "concept-0", "hook": "h"})
    prompt = stages._video_prompt(item, "Operator note.", stage="talking-head")
    assert prompt.startswith("Operator note.")


async def test_node_drop_marks_item_dropped():
    item = new_item({"id": "concept-0", "hook": "h"})
    result = await stages.node_drop(item, {"configurable": {}})
    assert result == {"dropped": True}


# ------------------------------------------------------------------ #
# node_scripts — escreve script por conceito (batch, antes do creator) #
# ------------------------------------------------------------------ #

async def test_node_scripts_writes_script_per_concept():
    seen: list[tuple[dict, str, str]] = []

    class _ScriptAdapter:
        async def write_script(self, *, concept, creator_ref, platform):
            seen.append((concept, creator_ref, platform))
            return f"SCRIPT for {concept['id']} ({platform})"

    config = {"configurable": {"adapter": _ScriptAdapter(), "run": {"platform": "reels"}}}
    state = {"concepts": [{"id": "c-0", "hook": "h0"}, {"id": "c-1", "hook": "h1"}]}

    result = await stages.node_scripts(state, config)

    # ordem preservada; script gravado em cada concept
    assert [c["id"] for c in result["concepts"]] == ["c-0", "c-1"]
    assert result["concepts"][0]["script"] == "SCRIPT for c-0 (reels)"
    assert result["concepts"][1]["script"] == "SCRIPT for c-1 (reels)"
    # creator ainda não existe → creator_ref genérico; platform propagado
    assert all(ref == "creator" and plat == "reels" for _, ref, plat in seen)


# ------------------------------------------------------------------ #
# node_concept_review — gate de edição (passthrough / resume / exclude)#
# ------------------------------------------------------------------ #

async def test_node_concept_review_passthrough_when_flag_off():
    config = {"configurable": {"run": {"edit_concepts": False}}}
    state = {"concepts": [{"id": "c-0", "script": "s"}]}
    assert await stages.node_concept_review(state, config) == {}


async def test_node_concept_review_passthrough_when_no_concepts():
    config = {"configurable": {"run": {"edit_concepts": True}}}
    assert await stages.node_concept_review({"concepts": []}, config) == {}


async def test_node_concept_review_replaces_with_edited_and_excluded(monkeypatch):
    # Usuário editou o script de c-0 e EXCLUIU c-1 (só c-0 volta no resume).
    monkeypatch.setattr(
        stages, "interrupt",
        lambda payload: {"concepts": [{"id": "c-0", "script": "EDITED"}]},
    )
    config = {"configurable": {"run": {"edit_concepts": True}}}
    state = {"concepts": [{"id": "c-0", "script": "orig"}, {"id": "c-1", "script": "orig"}]}

    result = await stages.node_concept_review(state, config)

    assert [c["id"] for c in result["concepts"]] == ["c-0"]
    assert result["concepts"][0]["script"] == "EDITED"


async def test_node_concept_review_keeps_concepts_when_no_decision(monkeypatch):
    # Decisão sem "concepts" (None) → mantém a lista original intacta.
    monkeypatch.setattr(stages, "interrupt", lambda payload: {})
    config = {"configurable": {"run": {"edit_concepts": True}}}
    state = {"concepts": [{"id": "c-0", "script": "orig"}]}

    assert await stages.node_concept_review(state, config) == {}
