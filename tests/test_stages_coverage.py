"""Cobertura dos ramos de borda em nodes/stages.py e adapters/base.py.

Foco nos caminhos que os testes de fluxo (graph e2e, reroll) não exercitam:
voice-preview best-effort, perfil de voz inválido, merge de roster sem update,
falhas parciais/totais no node_roster, aprovação sem seleção, e node_drop.
"""
from __future__ import annotations

import pytest

from orchestrator.adapters import base
from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters.mock import MockAdapter
from orchestrator.graph.state import Artifact, Item, QCResult, new_item
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


@pytest.mark.asyncio
async def test_node_roster_seed_reconstructs_reference_from_local_media(tmp_path, monkeypatch):
    """Reutilizar um creator cuja imagem só existe como path local /media/... deve
    dar ao fan-out uma referência buscável pelo provider (data: URI reconstruído do
    disco) — senão o adapter de vídeo real ignora a referência e gera outra pessoa."""
    import base64

    from orchestrator import media_store

    media_root = tmp_path / "media"
    face = media_root / "web-old" / "creator-0" / "image.png"
    face.parent.mkdir(parents=True)
    face.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    monkeypatch.setattr(stages, "default_media_path", lambda: media_root)

    seed = {
        "creator_id": "creator-0",
        # Só o path local servível — como sai do store de um creator real.
        "image_uri": "/media/web-old/creator-0/image.png",
        "voice_ref": "el_voice_0",
    }

    class _BoomAdapter:
        async def build_creator(self, *, index, system_prompt, voice_profile):
            raise AssertionError("build_creator should not be called for seed creator")

    config = {
        "configurable": {
            "adapter": _BoomAdapter(),
            "pipeline": {"roster": {"creators": 2}},
            "run": {"seed_creator": seed},
            "thread_id": "run-reuse",
        }
    }

    result = await stages.node_roster({}, config)

    creator = result["roster"][0]
    # A referência que o fan-out escolhe (`image_source_uri or upscaled_base`) precisa
    # ser buscável pelo provider — não o path /media local.
    ref = creator["image_source_uri"]
    assert ref.startswith("data:image/png;base64,")
    assert media_store._is_downloadable(ref)
    assert base64.b64decode(ref.split(",", 1)[1]) == b"\x89PNG\r\n\x1a\nFAKE"


@pytest.mark.asyncio
async def test_node_roster_seed_keeps_remote_reference_untouched(monkeypatch):
    """Se o seed já tem uma referência buscável (data:/http), não reconstrói nada."""
    seed = {
        "creator_id": "creator-fixed",
        "image_uri": "data:image/png;base64,SEED",
        "voice_ref": "voice-fixed",
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
    assert result["roster"][0]["image_source_uri"] == "data:image/png;base64,SEED"


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
# node_upscale — upscale do vídeo final (pós-montagem)                #
# ------------------------------------------------------------------ #

def _assembled_item() -> Item:
    item = new_item({"id": "concept-0", "hook": "h"})
    return item.model_copy(update={
        "assembled": Artifact(kind="video", uri="data:video/mp4;base64,QUJD", meta={"platform": "tiktok"}),
    })


def _upscale_config(adapter) -> dict:
    return {"configurable": {"adapter": adapter, "run": {}, "thread_id": "run-x"}}


async def test_node_upscale_replaces_final_with_upscaled(monkeypatch, tmp_path):
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    class _Upscaler:
        async def upscale(self, media_uri):
            return "data:video/mp4;base64,VVBTQ0FMRUQ="

    result = await stages.node_upscale(_assembled_item(), _upscale_config(_Upscaler()))
    art = result["assembled"]
    assert art is not None
    assert art.meta.get("upscaled") is True
    assert art.meta.get("upscaled_from") == "data:video/mp4;base64,QUJD"


async def test_node_upscale_noop_for_passthrough():
    class _Passthrough:
        async def upscale(self, media_uri):
            return media_uri  # inalterada

    result = await stages.node_upscale(_assembled_item(), _upscale_config(_Passthrough()))
    assert result == {}  # nada muda → não repersiste


async def test_node_upscale_skips_when_no_assembled():
    item = new_item({"id": "concept-0", "hook": "h"})  # assembled None

    class _Boom:
        async def upscale(self, media_uri):
            raise AssertionError("não deve ser chamado sem assembled")

    assert await stages.node_upscale(item, _upscale_config(_Boom())) == {}


async def test_node_upscale_best_effort_on_failure():
    class _Boom:
        async def upscale(self, media_uri):
            raise RuntimeError("upscaler fora do ar")

    result = await stages.node_upscale(_assembled_item(), _upscale_config(_Boom()))
    assert result == {}  # preserva o vídeo montado, não derruba o item


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


# ------------------------------------------------------------------ #
# node_assembly — resiliência: falha do assembler não mata o item     #
# ------------------------------------------------------------------ #

def _assembly_item() -> Item:
    """Item já com clip gerado e QC aprovado, pronto p/ montagem."""
    item = new_item({"id": "concept-0", "hook": "h", "offer": "serum X"})
    return item.model_copy(update={
        "clips": [Artifact(
            kind="clip",
            uri="/videos/run-x/items/concept-0/clip-0.mp4",
            meta={"tier": "ltx", "cost_usd": 0.08},
        )],
        "qc": QCResult(passed=True, score=1.0, reasons=[]),
    })


def _assembly_config(adapter, *, allow_mock_fallback: bool = False) -> dict:
    return {
        "configurable": {
            "adapter": adapter,
            "pipeline": {"assembly": {"allow_mock_fallback": allow_mock_fallback}},
            "run": {"platform": "tiktok"},
            "thread_id": "run-x",
        }
    }


class _BoomAssembler:
    async def assemble(self, **kwargs):
        raise RuntimeError(
            "Seedance bridge failed: input image may contain real person"
        )


async def test_node_assembly_surfaces_error_and_does_not_raise():
    result = await stages.node_assembly(_assembly_item(), _assembly_config(_BoomAssembler()))
    assert result["assembled"] is None
    assert "real person" in result["error"]
    # Não toca em clips: o reducer preserva os clips já gerados.
    assert "clips" not in result


async def test_node_assembly_treats_invalid_shape_as_error():
    class _BadAssembler:
        async def assemble(self, **kwargs):
            return None  # shape inválida — precisa virar erro, não estourar

    result = await stages.node_assembly(_assembly_item(), _assembly_config(_BadAssembler()))
    assert result["assembled"] is None
    assert result["error"]


async def test_node_assembly_success_clears_error(monkeypatch, tmp_path):
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)
    result = await stages.node_assembly(_assembly_item(), _assembly_config(MockAdapter(tiers=[])))
    assert result["assembled"] is not None
    assert result.get("error") is None


async def test_node_assembly_accepts_dict_shaped_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    class _DictAssembler:
        async def assemble(self, **kwargs):
            return {"kind": "video", "uri": "data:video/mp4;base64,AAAA", "meta": {}}

    result = await stages.node_assembly(_assembly_item(), _assembly_config(_DictAssembler()))
    assert result["assembled"] is not None
    assert result.get("error") is None


async def test_node_assembly_dict_without_uri_is_error():
    class _BadDictAssembler:
        async def assemble(self, **kwargs):
            return {"kind": "video"}  # sem uri → shape inválida

    result = await stages.node_assembly(_assembly_item(), _assembly_config(_BadDictAssembler()))
    assert result["assembled"] is None
    assert result["error"]


async def test_node_assembly_mock_fallback_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)
    result = await stages.node_assembly(
        _assembly_item(), _assembly_config(_BoomAssembler(), allow_mock_fallback=True)
    )
    assert result["assembled"] is not None
    assert result["assembled"].meta.get("fallback_reason") == "assembly_gateway_rejected"
    assert result["assembled"].meta.get("provider") == "mock"
    assert result.get("error") is None
