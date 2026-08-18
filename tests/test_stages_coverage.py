"""Cobertura dos ramos de borda em nodes/stages.py e adapters/base.py.

Foco nos caminhos que os testes de fluxo (graph e2e, reroll) não exercitam:
voice-preview best-effort, perfil de voz inválido, merge de roster sem update,
falhas parciais/totais no node_roster, aprovação sem seleção, e node_drop.
"""
from __future__ import annotations

import pytest

from orchestrator.adapters import base
from orchestrator.adapters.base import RenderedMedia, VoiceProfile
from orchestrator.adapters.mock import MockAdapter
from orchestrator.graph.state import Artifact, Item, QCResult, new_item
from orchestrator.nodes import stages
from orchestrator.storage.base import StoredObject, is_downloadable

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
    assert is_downloadable(ref)
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


async def test_node_approval_is_a_passthrough_when_the_legacy_gate_is_disabled():
    config = {"configurable": {"run": {"approve_creators": False}}}

    assert await stages.node_approval(
        {"roster": [{"id": "creator-0"}]},
        config,
    ) == {}


async def test_creative_progress_propagates_unexpected_runtime_errors(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("event transport failed")

    monkeypatch.setattr(stages, "adispatch_custom_event", fail)

    with pytest.raises(RuntimeError, match="event transport failed"):
        await stages._report_creative_progress(
            {"configurable": {}},
            stage_id="scripts",
            completed_units=1,
            total_units=1,
        )


def test_apply_roster_updates_normalizes_image_and_voice_preview_aliases():
    roster = [{"id": "creator-0", "upscaled_base": "mock://old"}]

    updated = stages.apply_roster_updates(
        roster,
        [
            {
                "id": "creator-0",
                "image_uri": "mock://new",
                "voice_preview": "data:audio/wav;base64,AAAA",
            }
        ],
    )

    assert updated[0]["upscaled_base"] == "mock://new"
    assert updated[0]["image"] == "mock://new"
    assert updated[0]["voice_preview_uri"] == "data:audio/wav;base64,AAAA"


def test_review_creator_updates_reject_unknown_fields():
    roster = [{"id": "creator-0"}, {"id": "creator-1"}]

    with pytest.raises(ValueError, match="unsupported creator review fields"):
        stages.apply_review_creator_updates(
            roster,
            [
                {"id": "creator-0", "system_prompt": "leak"},
                {"id": "creator-1"},
            ],
        )


def test_prompt_with_persona_accepts_persona_without_an_operator_prompt():
    assert stages._prompt_with_persona(" Busy parent ", None) == "Busy parent"


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


async def test_node_upscale_propagates_stage_execution_error():
    """A3: erro de config (catálogo sem o stage) não pode virar no-op best-effort."""
    from orchestrator.agent_catalog import AgentCatalog
    from orchestrator.stage_executor import StageExecutionError

    class _Upscaler:
        async def upscale(self, media_uri):
            raise AssertionError("não deve ser chamado com catálogo inválido")

    config = _upscale_config(_Upscaler())
    config["configurable"]["agent_catalog"] = AgentCatalog(stages=())  # stage 'upscale' ausente

    with pytest.raises(StageExecutionError, match="not configured"):
        await stages.node_upscale(_assembled_item(), config)


async def test_node_assembly_propagates_stage_execution_error(monkeypatch, tmp_path):
    """A3: mesma regra para assembly — erro de config estoura, não vira erro por-item."""
    from orchestrator.agent_catalog import AgentCatalog
    from orchestrator.stage_executor import StageExecutionError

    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    class _Assembler:
        async def assemble(self, **kwargs):
            raise AssertionError("não deve ser chamado com catálogo inválido")

    config = {
        "configurable": {
            "adapter": _Assembler(),
            "pipeline": {},
            "run": {"platform": "tiktok"},
            "thread_id": "run-x",
            "agent_catalog": AgentCatalog(stages=()),  # stage 'assembly' ausente
        }
    }

    with pytest.raises(StageExecutionError, match="not configured"):
        await stages.node_assembly(_assembled_item(), config)


# ------------------------------------------------------------------ #
# node_scripts — escreve script por conceito (batch, antes do creator) #
# ------------------------------------------------------------------ #

async def test_node_scripts_writes_script_per_concept():
    seen: list[tuple[dict, str, str]] = []

    class _ScriptAdapter:
        async def write_script(self, *, concept, creator_ref, platform, revision=None):
            seen.append((concept, creator_ref, platform))
            return f"HOOK: SCRIPT for {concept['id']} ({platform}) with extra spoken words to easily satisfy the server minimum requirement of twenty eight words for script duration validation in test suite execution right here now."

    config = {
        "configurable": {
            "adapter": object(),
            "language_runtime": _ScriptAdapter(),
            "run": {"platform": "reels"},
        }
    }

    state = {"concepts": [{"id": "c-0", "hook": "h0"}, {"id": "c-1", "hook": "h1"}]}

    result = await stages.node_scripts(state, config)

    # ordem preservada; script gravado em cada concept
    assert [c["id"] for c in result["concepts"]] == ["c-0", "c-1"]
    assert "c-0" in result["concepts"][0]["script"]
    assert "c-1" in result["concepts"][1]["script"]
    # creator ainda não existe → creator_ref genérico; platform propagado
    assert all(ref == "creator" and plat == "reels" for _, ref, plat in seen)


async def test_node_scripts_accepts_a_serialized_script_contract(monkeypatch):
    async def execute(*_args, **_kwargs):
        return {
            "script": "HOOK: Serialized\nCTA: Buy",
            "script_draft": {
                "id": "run-script-01",
                "concept_id": "c-0",
                "spoken_beats": [
                    {"section": "hook", "text": "Serialized", "seconds": 2},
                    {"section": "cta", "text": "Buy", "seconds": 2},
                ],
                "visual_beats": ["Creator to camera"],
                "on_screen_text": [],
                "call_to_action": "Buy",
                "estimated_duration": 4,
            },
        }

    monkeypatch.setattr(stages, "execute_stage_tool", execute)
    config = {
        "configurable": {
            "adapter": object(),
            "run": {"platform": "tiktok"},
        }
    }
    state = {
        "run_id": "run",
        "campaign": {
            "offer": "Serum X",
            "audience": "Adults",
            "batch_size": 1,
        },
        "concepts": [{"id": "c-0", "hook": "Hook"}],
    }

    result = await stages.node_scripts(state, config)

    assert result["concepts"][0]["script_draft"]["id"] == "run-script-01"


async def test_node_scripts_applies_server_owned_narration_budget(monkeypatch):
    captured: dict = {}

    async def execute(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "script": "HOOK: Short\nCTA: Rent now",
            "script_draft": {
                "id": "run-script-01",
                "concept_id": "c-0",
                "spoken_beats": [
                    {"section": "hook", "text": "Short", "seconds": 2},
                    {"section": "cta", "text": "Rent now", "seconds": 2},
                ],
                "visual_beats": ["Creator to camera"],
                "on_screen_text": [],
                "call_to_action": "Rent now",
                "estimated_duration": 4,
            },
        }

    monkeypatch.setattr(stages, "execute_stage_tool", execute)
    config = {
        "configurable": {
            "adapter": object(),
            "pipeline": {
                "assembly": {
                    "narration_target_seconds": 16,
                    "narration_min_words": 28,
                    "narration_max_words": None,
                },
            },
            "run": {"platform": "tiktok"},
        },
    }
    state = {
        "run_id": "run",
        "campaign": {
            "offer": "Chair rental",
            "audience": "Adults",
            "batch_size": 1,
        },
        "concepts": [{"id": "c-0", "hook": "Hook"}],
    }

    await stages.node_scripts(state, config)

    assert captured["target_duration_seconds"] == 16
    assert captured["min_spoken_words"] == 28
    assert captured["max_spoken_words"] is None


async def test_node_creator_profiles_accepts_a_serialized_roster(monkeypatch):
    async def execute(*_args, **_kwargs):
        return {
            "creators": [
                {
                    "id": "creator-0",
                    "archetype": "Guide",
                    "visual_brief": "Adult guide",
                    "voice_brief": "Warm",
                    "performance_style": "Calm",
                    "exclusions": [],
                },
                {
                    "id": "creator-1",
                    "archetype": "Tester",
                    "visual_brief": "Adult tester",
                    "voice_brief": "Direct",
                    "performance_style": "Fast",
                    "exclusions": [],
                },
            ],
            "assignments": [
                {"concept_id": "c-0", "creator_id": "creator-0"},
            ],
        }

    monkeypatch.setattr(stages, "execute_stage_tool", execute)
    config = {"configurable": {"adapter": object(), "run": {}}}
    state = {
        "campaign": {
            "offer": "Serum X",
            "audience": "Adults",
            "batch_size": 1,
        },
        "concepts": [{"id": "c-0"}],
    }

    result = await stages.node_creator_profiles(state, config)

    assert [creator["id"] for creator in result["creator_profiles"]] == [
        "creator-0",
        "creator-1",
    ]


async def test_node_review_routes_regeneration_and_rejects_invalid_decisions(monkeypatch):
    config = {"configurable": {"run": {"review_plan": True}}}
    state = {
        "concepts": [{"id": "concept-1"}],
        "roster": [{"id": "creator-0"}, {"id": "creator-1"}],
    }

    monkeypatch.setattr(
        stages,
        "interrupt",
        lambda _payload: {
            "action": "regenerate",
            "target": "scripts",
            "ids": ["concept-1"],
            "feedback": "Shorter hook",
        },
    )
    assert await stages.node_review(state, config) == {
        "review_approved": False,
        "revision_request": {
            "target": "scripts",
            "ids": ["concept-1"],
            "feedback": "Shorter hook",
        },
    }

    monkeypatch.setattr(
        stages,
        "interrupt",
        lambda _payload: {"action": "regenerate", "target": "unknown"},
    )
    with pytest.raises(ValueError, match="regenerate target"):
        await stages.node_review(state, config)

    monkeypatch.setattr(
        stages,
        "interrupt",
        lambda _payload: {"action": "delete"},
    )
    with pytest.raises(ValueError, match="review action"):
        await stages.node_review(state, config)

    voice_state = {
        "concepts": [{"id": "concept-1"}],
        "roster": [
            {
                "id": "creator-0",
                "voice_brief": "Warm",
                "voice_candidates": [{"candidate_id": "candidate-0"}],
                "selected_voice_candidate_id": "candidate-0",
            },
            {
                "id": "creator-1",
                "voice_brief": "Direct",
                "voice_candidates": [{"candidate_id": "candidate-1"}],
                "selected_voice_candidate_id": "candidate-1",
            },
        ],
    }
    monkeypatch.setattr(
        stages,
        "interrupt",
        lambda _payload: {
            "action": "regenerate",
            "target": "voices",
            "ids": ["creator-0"],
            "creators": [
                {"id": "creator-0", "voice_brief": "Deeper and slower"},
                {"id": "creator-1", "voice_brief": "Direct"},
            ],
        },
    )
    reroll = await stages.node_review(voice_state, config)
    assert reroll["roster"][0]["voice_brief"] == "Deeper and slower"
    assert reroll["roster"][0]["voice_candidates"] == []
    assert reroll["roster"][0]["selected_voice_candidate_id"] is None
    assert reroll["roster"][1]["selected_voice_candidate_id"] == "candidate-1"


async def test_node_review_requires_one_valid_voice_candidate_per_creator(monkeypatch):
    roster = [
        {
            "id": f"creator-{index}",
            "voice_candidates": [
                {
                    "candidate_id": f"candidate-{index}",
                    "preview": {
                        "kind": "voice_preview",
                        "uri": f"r2://candidate-{index}.mp3",
                    },
                    "duration_seconds": 3.0,
                }
            ],
            "selected_voice_candidate_id": None,
        }
        for index in range(2)
    ]
    monkeypatch.setattr(stages, "interrupt", lambda _payload: {"action": "approve"})

    with pytest.raises(ValueError, match="select one voice candidate"):
        await stages.node_review(
            {"concepts": [{"id": "concept-1"}], "roster": roster},
            {"configurable": {"run": {"review_plan": True}}},
        )


async def test_node_review_preserves_concept_edits_before_regeneration(monkeypatch):
    monkeypatch.setattr(
        stages,
        "interrupt",
        lambda _payload: {
            "action": "regenerate",
            "target": "scripts",
            "ids": ["concept-1"],
            "concepts": [{"id": "concept-1", "script": "Edited before rerun"}],
        },
    )

    result = await stages.node_review(
        {"concepts": [{"id": "concept-1", "script": "Original"}], "roster": []},
        {"configurable": {"run": {"review_plan": True}}},
    )

    assert result["concepts"][0]["script"] == "Edited before rerun"


async def test_node_finalize_voices_uses_only_selected_candidate(monkeypatch):
    calls = []

    async def execute(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "provider": "elevenlabs",
            "voice_ref": "voice-permanent-1",
            "selected_candidate_id": "candidate-1",
            "preview_uri": "https://temporary.example/preview.mp3",
            "design_model": "eleven_ttv_v3",
            "tts_model": "eleven_turbo_v2_5",
        }

    monkeypatch.setattr(stages, "execute_stage_tool", execute)
    candidates = [
        {
            "candidate_id": f"candidate-{index}",
            "preview": {
                "kind": "voice_preview",
                "uri": f"r2://bucket/candidate-{index}.mp3",
            },
            "duration_seconds": 3.0,
        }
        for index in range(2)
    ]
    batch = {
        "provider": "elevenlabs",
        "design_model": "eleven_ttv_v3",
        "description_hash": "description-hash",
        "prompt_version": "voice-match-v1",
        "candidates": candidates,
        "cost_usd": 0.02,
        "cost_source": "estimate",
    }
    state = {
        "roster": [
            {
                "id": "creator-0",
                "voice_candidates": candidates,
                "voice_design_batch": batch,
                "selected_voice_candidate_id": "candidate-1",
            }
        ],
        "creator_assignments": [
            {"concept_id": "concept-0", "creator_id": "creator-0"}
        ],
    }
    config = {
        "configurable": {
            "adapter": object(),
            "organization_id": "org-1",
            "thread_id": "run-1",
            "run": {},
        }
    }

    result = await stages.node_finalize_voices(state, config)

    assert len(calls) == 1
    assert calls[0]["candidate_id"] == "candidate-1"
    creator = result["roster"][0]
    assert creator["voice_ref"] == "voice-permanent-1"
    assert creator["voice_id"] == "voice-permanent-1"
    assert creator["voice_preview_uri"] == "r2://bucket/candidate-1.mp3"


async def test_node_finalize_voices_blocks_missing_selection_or_empty_voice(monkeypatch):
    state = {
        "roster": [
            {
                "id": "creator-0",
                "voice_candidates": [
                    {
                        "candidate_id": "candidate-0",
                        "preview": {"kind": "voice_preview", "uri": "r2://preview.mp3"},
                        "duration_seconds": 3.0,
                    }
                ],
            }
        ],
        "creator_assignments": [{"creator_id": "creator-0"}],
    }
    config = {
        "configurable": {"adapter": object(), "run": {}, "thread_id": "run-1"}
    }

    with pytest.raises(ValueError, match="selected voice candidate"):
        await stages.node_finalize_voices(state, config)

    state["roster"][0]["selected_voice_candidate_id"] = "candidate-0"
    state["roster"][0]["voice_design_batch"] = {
        "provider": "elevenlabs",
        "design_model": "eleven_ttv_v3",
        "description_hash": "description-hash",
        "prompt_version": "voice-match-v1",
        "candidates": state["roster"][0]["voice_candidates"],
        "cost_usd": 0.01,
        "cost_source": "estimate",
    }

    async def empty_voice(*_args, **_kwargs):
        return {"voice_ref": ""}

    monkeypatch.setattr(stages, "execute_stage_tool", empty_voice)
    with pytest.raises(ValueError, match="empty voice_ref"):
        await stages.node_finalize_voices(state, config)


async def test_node_finalize_voices_requires_design_batch() -> None:
    state = {
        "roster": [
            {
                "id": "creator-0",
                "selected_voice_candidate_id": "candidate-0",
                "voice_candidates": [
                    {
                        "candidate_id": "candidate-0",
                        "preview": {"uri": "r2://preview.mp3"},
                    }
                ],
            }
        ]
    }
    config = {
        "configurable": {"adapter": object(), "run": {}, "thread_id": "run-1"}
    }

    with pytest.raises(ValueError, match="no voice design batch"):
        await stages.node_finalize_voices(state, config)


async def test_node_finalize_voices_requires_canonical_selected_preview(monkeypatch):
    async def finalize(*_args, **_kwargs):
        return {"voice_ref": "voice-permanent", "provider": "elevenlabs"}

    monkeypatch.setattr(stages, "execute_stage_tool", finalize)
    candidate = {"candidate_id": "candidate-0", "preview": {}}
    state = {
        "roster": [
            {
                "id": "creator-0",
                "selected_voice_candidate_id": "candidate-0",
                "voice_candidates": [candidate],
                "voice_design_batch": {
                    "description_hash": "hash",
                    "candidates": [candidate],
                },
            }
        ]
    }
    config = {
        "configurable": {"adapter": object(), "run": {}, "thread_id": "run-1"}
    }

    with pytest.raises(ValueError, match="no voice preview URI"):
        await stages.node_finalize_voices(state, config)


async def test_node_finalize_voices_blocks_missing_assigned_creator(monkeypatch):
    async def finalize(*_args, **_kwargs):
        return {"voice_ref": "voice-permanent", "provider": "elevenlabs"}

    monkeypatch.setattr(stages, "execute_stage_tool", finalize)
    candidate = {
        "candidate_id": "candidate-0",
        "preview": {"uri": "r2://preview.mp3"},
    }
    state = {
        "roster": [
            {
                "id": "creator-0",
                "selected_voice_candidate_id": "candidate-0",
                "voice_candidates": [candidate],
                "voice_design_batch": {
                    "description_hash": "hash",
                    "candidates": [candidate],
                },
            }
        ],
        "creator_assignments": [
            {"creator_id": "creator-0"},
            {"creator_id": "creator-missing"},
        ],
    }
    config = {
        "configurable": {"adapter": object(), "run": {}, "thread_id": "run-1"}
    }

    with pytest.raises(ValueError, match="creator-missing"):
        await stages.node_finalize_voices(state, config)


async def test_node_voice_candidates_rerolls_only_requested_creator(monkeypatch):
    designed_for = []

    async def execute(*_args, **kwargs):
        if kwargs["tool_name"] == "derive_creator_voice_spec":
            designed_for.append(kwargs["profile"]["id"])
            return {
                "language_code": "pt-BR",
                "accent": "neutral",
                "vocal_presentation": "neutral",
                "vocal_age": "adult",
                "timbre": "warm",
                "pace": "conversational",
                "energy": "balanced",
            }
        creator_id = designed_for[-1]
        return {
            "provider": "elevenlabs",
            "design_model": "eleven_ttv_v3",
            "description_hash": f"hash-{creator_id}",
            "prompt_version": "voice-match-v1",
            "candidates": [
                {
                    "candidate_id": f"new-{creator_id}",
                    "preview": {
                        "kind": "voice_preview",
                        "uri": "data:audio/mpeg;base64,QUJD",
                    },
                    "duration_seconds": 3.0,
                }
            ],
            "cost_usd": 0.01,
            "cost_source": "estimate",
        }

    async def persist(candidates, **_kwargs):
        return candidates

    monkeypatch.setattr(stages, "execute_stage_tool", execute)
    monkeypatch.setattr(stages.media_store, "persist_voice_candidates", persist)
    untouched = {
        "id": "creator-1",
        "upscaled_base": "r2://creator-1.png",
        "voice_candidates": [{"candidate_id": "old-1"}],
        "selected_voice_candidate_id": "old-1",
    }
    state = {
        "run_id": "run-1",
        "revision_request": {"target": "voices", "ids": ["creator-0"]},
        "roster": [
            {
                "id": "creator-0",
                "upscaled_base": "r2://creator-0.png",
                "voice_reroll_count": 0,
                "voice_candidates": [{"candidate_id": "old-0"}],
                "selected_voice_candidate_id": "old-0",
            },
            untouched,
        ],
    }
    config = {
        "configurable": {
            "adapter": object(),
            "pipeline": {
                "voice": {
                    "max_rerolls_per_creator": 2,
                    "selection_without_review": "first",
                }
            },
            "run": {"review_plan": True},
            "thread_id": "run-1",
        }
    }

    result = await stages.node_voice_candidates(state, config)

    assert designed_for == ["creator-0"]
    assert result["roster"][0]["voice_reroll_count"] == 1
    assert result["roster"][0]["selected_voice_candidate_id"] is None
    assert result["roster"][0]["voice_design_history"][0][0]["candidate_id"] == "old-0"
    assert result["roster"][0]["upscaled_base"] == "r2://creator-0.png"
    assert result["roster"][1] == untouched
    assert result["total_cost_usd"] == 0.01


async def test_node_voice_candidates_enforces_reroll_limit(monkeypatch):
    async def should_not_execute(*_args, **_kwargs):
        raise AssertionError("paid voice design must not run after the reroll limit")

    monkeypatch.setattr(stages, "execute_stage_tool", should_not_execute)
    state = {
        "revision_request": {"target": "voices", "ids": ["creator-0"]},
        "roster": [{"id": "creator-0", "voice_reroll_count": 2}],
    }
    config = {
        "configurable": {
            "adapter": object(),
            "pipeline": {"voice": {"max_rerolls_per_creator": 2}},
            "run": {"review_plan": True},
        }
    }

    with pytest.raises(ValueError, match="reroll limit"):
        await stages.node_voice_candidates(state, config)


async def test_node_voice_candidates_rejects_foreign_or_empty_reroll_ids(monkeypatch):
    async def should_not_execute(*_args, **_kwargs):
        raise AssertionError("invalid reroll must fail before any tool")

    monkeypatch.setattr(stages, "execute_stage_tool", should_not_execute)
    config = {
        "configurable": {
            "adapter": object(),
            "pipeline": {"voice": {"max_rerolls_per_creator": 2}},
            "run": {"review_plan": True},
        }
    }

    for ids in ([], ["creator-other"]):
        with pytest.raises(ValueError, match="must belong to the current creator roster"):
            await stages.node_voice_candidates(
                {
                    "revision_request": {"target": "voices", "ids": ids},
                    "roster": [{"id": "creator-0"}],
                },
                config,
            )


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


def test_narration_text_removes_script_section_labels():
    assert stages._narration_text(
        "HOOK: Isso mudou minha rotina.\n"
        "BODY: Agora eu mostro como funciona.\n"
        "CTA: Veja a oferta."
    ) == (
        "Isso mudou minha rotina. "
        "Agora eu mostro como funciona. "
        "Veja a oferta."
    )


async def test_node_voiceover_uses_approved_voice_persists_audio_and_adds_cost(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    class _VoiceoverAdapter:
        def __init__(self):
            self.calls = []

        async def synthesize_voiceover(self, **kwargs):
            self.calls.append(kwargs)
            return Artifact(
                kind="voiceover",
                uri="data:audio/mpeg;base64,SUQz",
                meta={"provider": "replicate", "cost_usd": 0.0042},
            )

    adapter = _VoiceoverAdapter()
    item = _assembly_item().model_copy(
        update={
            "creator_voice_ref": "Rachel",
            "script": "HOOK: Isso mudou.\nBODY: Veja como.\nCTA: Saiba mais.",
            "cost_usd": 0.16,
        }
    )

    result = await stages.node_voiceover(item, _assembly_config(adapter))

    assert adapter.calls == [
        {
            "voice_ref": "Rachel",
            "text": "Isso mudou. Veja como. Saiba mais.",
        }
    ]
    assert result["voiceover"].uri.endswith("/voiceover.mp3")
    assert result["cost_usd"] == pytest.approx(0.1642)
    assert result["error"] is None


async def test_node_voiceover_fails_explicitly_without_approved_voice():
    result = await stages.node_voiceover(
        _assembly_item().model_copy(update={"script": "HOOK: Texto"}),
        _assembly_config(object()),
    )

    assert result["voiceover"] is None
    assert "approved creator voice" in result["error"]


async def test_node_voiceover_fails_explicitly_without_approved_script():
    result = await stages.node_voiceover(
        _assembly_item().model_copy(
            update={"creator_voice_ref": "Rachel", "script": None}
        ),
        _assembly_config(object()),
    )

    assert result["voiceover"] is None
    assert "approved script" in result["error"]


async def test_node_voiceover_surfaces_provider_failure():
    class _BrokenVoiceover:
        async def synthesize_voiceover(self, **kwargs):
            raise RuntimeError("TTS unavailable")

    result = await stages.node_voiceover(
        _assembly_item().model_copy(
            update={"creator_voice_ref": "Rachel", "script": "HOOK: Texto"}
        ),
        _assembly_config(_BrokenVoiceover()),
    )

    assert result["voiceover"] is None
    assert result["error"] == "voiceover: TTS unavailable"


async def test_node_voiceover_propagates_stage_configuration_error(monkeypatch):
    async def misconfigured(*args, **kwargs):
        raise stages.StageExecutionError("voiceover stage is not configured")

    monkeypatch.setattr(stages, "execute_stage_tool", misconfigured)
    item = _assembly_item().model_copy(
        update={"creator_voice_ref": "Rachel", "script": "HOOK: Texto"}
    )

    with pytest.raises(stages.StageExecutionError, match="not configured"):
        await stages.node_voiceover(item, _assembly_config(object()))


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


async def test_node_assembly_adds_provider_cost_to_item_total(monkeypatch, tmp_path):
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    class _CostedAssembler:
        async def assemble(self, **kwargs):
            return Artifact(
                kind="video",
                uri="data:video/mp4;base64,AAAA",
                meta={"provider": "replicate", "cost_usd": 0.08},
            )

    item = _assembly_item().model_copy(update={"cost_usd": 0.16})
    result = await stages.node_assembly(item, _assembly_config(_CostedAssembler()))

    assert result["cost_usd"] == pytest.approx(0.24)


async def test_node_assembly_persists_ffmpeg_bytes_without_exposing_them_in_state(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    class _RenderedAssembler:
        async def assemble(self, **kwargs):
            return RenderedMedia(
                data=b"\x00\x00\x00\x18ftypmp42-final",
                content_type="video/mp4",
                meta={
                    "provider": "ffmpeg",
                    "video_codec": "h264",
                    "audio_codec": "aac",
                    "cost_usd": 0.0,
                },
            )

    item = _assembly_item().model_copy(
        update={
            "voiceover": Artifact(
                kind="voiceover",
                uri="data:audio/mpeg;base64,SUQz",
            ),
            "cost_usd": 0.1642,
        }
    )

    result = await stages.node_assembly(
        item,
        _assembly_config(_RenderedAssembler()),
    )

    assert result["assembled"].uri == f"/videos/run-x/items/{item.id}/assembled.mp4"
    assert result["assembled"].meta["provider"] == "ffmpeg"
    assert result["assembled"].meta["audio_codec"] == "aac"
    assert result["cost_usd"] == pytest.approx(0.1642)
    assert (
        tmp_path / "run-x" / "items" / item.id / "assembled.mp4"
    ).read_bytes() == b"\x00\x00\x00\x18ftypmp42-final"


async def test_node_assembly_signs_r2_inputs_but_persists_only_canonical_pointer():
    class _R2Storage:
        backend = "r2"

        async def get_signed_url(self, key, *, ttl_seconds=900):
            return f"https://signed.example/{key}"

        async def put_bytes(self, data, *, key_base, content_type):
            key = f"{key_base}.mp4"
            return StoredObject(
                backend="r2",
                key=key,
                uri=f"r2://ugc/{key}",
                content_type=content_type,
                size_bytes=len(data),
                sha256="abc",
            )

        async def put_from_url(self, uri, *, key_base, client=None):
            return None

    class _InspectingAssembler:
        def __init__(self):
            self.item = None

        async def assemble(self, *, item, **kwargs):
            self.item = item
            return RenderedMedia(
                data=b"final",
                content_type="video/mp4",
                meta={"provider": "ffmpeg", "cost_usd": 0.0},
            )

    adapter = _InspectingAssembler()
    storage = _R2Storage()
    item = _assembly_item().model_copy(
        update={
            "clips": [
                Artifact(kind="clip", uri="r2://ugc/run/item/clip-0.mp4"),
                Artifact(kind="clip", uri="r2://ugc/run/item/clip-1.mp4"),
            ],
            "voiceover": Artifact(
                kind="voiceover",
                uri="r2://ugc/run/item/voiceover.mp3",
            ),
        }
    )
    config = _assembly_config(adapter)
    config["configurable"]["videos_storage"] = storage

    result = await stages.node_assembly(item, config)

    assert [clip.uri for clip in adapter.item.clips] == [
        "https://signed.example/run/item/clip-0.mp4",
        "https://signed.example/run/item/clip-1.mp4",
    ]
    assert (
        adapter.item.voiceover.uri
        == "https://signed.example/run/item/voiceover.mp3"
    )
    assert result["assembled"].uri.startswith("r2://ugc/")
    assert "signed.example" not in result["assembled"].model_dump_json()


async def test_node_assembly_resolves_local_video_urls_to_runtime_files(
    monkeypatch,
    tmp_path,
):
    clip_path = tmp_path / "run-x" / "items" / "item-x" / "clip-0.mp4"
    voice_path = tmp_path / "run-x" / "items" / "item-x" / "voiceover.mp3"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"clip")
    voice_path.write_bytes(b"voice")
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    item = _assembly_item().model_copy(
        update={
            "clips": [
                Artifact(
                    kind="clip",
                    uri="/videos/run-x/items/item-x/clip-0.mp4",
                ),
                Artifact(
                    kind="clip",
                    uri="/videos/run-x/items/item-x/clip-0.mp4",
                ),
            ],
            "voiceover": Artifact(
                kind="voiceover",
                uri="/videos/run-x/items/item-x/voiceover.mp3",
            ),
        }
    )

    resolved = stages._resolve_local_assembly_paths(item)

    assert resolved.clips[0].uri == str(clip_path)
    assert resolved.voiceover.uri == str(voice_path)


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
