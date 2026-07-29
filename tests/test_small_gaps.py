"""Cobertura dos ramos de borda restantes em módulos pequenos.

Um teste por lacuna: caminhos de erro, fallbacks e branches de client-próprio que
os testes de fluxo não exercitam.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from orchestrator import creator_store, feedback_store, media_store, prompt_store
from orchestrator.adapters import integrity_qc, mock, replicate_voice
from orchestrator.adapters._agent_loop import AgentRunResult, ToolAttempt, ToolCall
from orchestrator.adapters.base import VoiceProfile
from orchestrator.graph.state import Artifact, Item
from orchestrator.graph.routing import select_tier
from orchestrator.registry import resolve_adapter


# ------------------------------------------------------------------ #
# graph/routing                                                     #
# ------------------------------------------------------------------ #

def test_select_tier_raises_without_tiers():
    with pytest.raises(ValueError, match="sem tiers"):
        select_tier(0, [])


# ------------------------------------------------------------------ #
# registry                                                          #
# ------------------------------------------------------------------ #

def test_resolve_adapter_unknown_name_raises():
    with pytest.raises(KeyError, match="adapter desconhecido"):
        resolve_adapter("nope", {})


# ------------------------------------------------------------------ #
# adapters/integrity_qc                                             #
# ------------------------------------------------------------------ #

async def test_integrity_qc_flags_invalid_kind_and_uri():
    adapter = integrity_qc.IntegrityQCAdapter(required_clip_count=1)
    item = Item(
        id="i",
        concept={},
        clips=[Artifact(kind="image", uri="", meta={"provider": "replicate"})],
    )
    result = await adapter.qc_check(item)
    assert result.passed is False
    assert any("invalid_kind" in r for r in result.reasons)
    assert any("invalid_video_uri" in r for r in result.reasons)


def test_is_video_uri_false_for_empty():
    assert integrity_qc._is_video_uri("") is False


# ------------------------------------------------------------------ #
# adapters/mock                                                     #
# ------------------------------------------------------------------ #

async def test_mock_adapter_awaits_latency():
    adapter = mock.MockAdapter(tiers=[{"name": "ltx"}], latency=0.001)
    concepts = await adapter.generate_concepts(offer="x", n=1, seed="s")
    assert len(concepts) == 1


async def test_mock_qc_check_requires_item_id():
    adapter = mock.MockAdapter(tiers=[{"name": "ltx"}])
    with pytest.raises(ValueError, match="qc_check requires"):
        await adapter.qc_check(item=None)


async def test_mock_assemble_requires_item_id():
    adapter = mock.MockAdapter(tiers=[{"name": "ltx"}])
    with pytest.raises(ValueError, match="assemble requires"):
        await adapter.assemble(item=None)


def test_build_mock_adapter_passes_latency():
    adapter = mock.build_mock_adapter([{"name": "ltx"}], latency=0.5)
    assert adapter.latency == 0.5


def test_mock_agent_brain_includes_the_configured_system_prompt():
    brain = mock._MockAgentBrain(lambda *_args: None, system_prompt="safe prompt")

    messages = brain.initial_messages(
        "concepts",
        {},
        [{"name": "submit_concepts"}],
    )

    assert messages[0]["system_prompt"] == "safe prompt"


def test_mock_terminal_submission_rejects_an_unknown_stage():
    with pytest.raises(ValueError, match="unsupported terminal mock stage"):
        mock._terminal_submission("unknown", {})


async def test_mock_creative_outputs_change_deterministically_for_persona_and_revision():
    adapter = mock.MockAdapter(tiers=[])

    concepts = await adapter.generate_concepts(
        offer="Serum X",
        n=1,
        seed="run-1",
        revision="shorter",
    )
    script = await adapter.write_script(
        concepts[0],
        "creator-0",
        "tiktok",
        persona="Busy parent",
        revision="shorter",
    )

    assert concepts[0]["id"].startswith("concept-")
    assert "PERSONA_CONTEXT[" in script
    assert "REVISED[" in script


# ------------------------------------------------------------------ #
# adapters/replicate_voice                                          #
# ------------------------------------------------------------------ #

def test_load_base_input_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("REPLICATE_ELEVENLABS_INPUT_JSON", "{not json")
    with pytest.raises(RuntimeError, match="JSON válido"):
        replicate_voice._load_base_input()


def test_load_base_input_rejects_non_object(monkeypatch):
    monkeypatch.setenv("REPLICATE_ELEVENLABS_INPUT_JSON", "[1, 2, 3]")
    with pytest.raises(RuntimeError, match="objeto JSON"):
        replicate_voice._load_base_input()


def test_replicate_voice_rejects_empty_text_field():
    with pytest.raises(RuntimeError, match="TEXT_FIELD"):
        replicate_voice.ReplicateVoiceAdapter(model="owner/model", text_field="   ")


def test_replicate_voice_build_text_without_prompt():
    text = replicate_voice.ReplicateVoiceAdapter._build_text(
        2, VoiceProfile(preset="male", prompt="")
    )
    assert text == "creator voice 2 | preset=male"


# ------------------------------------------------------------------ #
# logging_config                                                    #
# ------------------------------------------------------------------ #

def test_configure_logging_adds_file_handler(monkeypatch, tmp_path):
    from orchestrator import logging_config

    log_file = tmp_path / "orch.log"
    monkeypatch.setenv("ORCHESTRATOR_LOG_FILE", str(log_file))
    try:
        logging_config.configure_logging()
        root = logging.getLogger()
        assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
    finally:
        # limpa os handlers que instalamos para não vazar file handles/globais
        root = logging.getLogger()
        for h in [h for h in root.handlers if getattr(h, "_orchestrator_handler", False)]:
            root.removeHandler(h)
            h.close()


def test_plain_logging_handler_uses_the_human_readable_formatter(monkeypatch):
    from orchestrator import logging_config

    monkeypatch.delenv("ORCHESTRATOR_LOG_FORMAT", raising=False)
    handler = logging_config._make_handler(logging.StreamHandler())

    assert handler.formatter is not None
    assert "%(levelname)-7s" in handler.formatter._fmt


# ------------------------------------------------------------------ #
# trace sanitization                                                #
# ------------------------------------------------------------------ #

def test_trace_sanitizer_redacts_a_direct_creative_field(monkeypatch):
    from orchestrator.tracing import _sanitize_trace_payload

    monkeypatch.setenv("LANGSMITH_REDACT_PROMPTS", "true")

    assert _sanitize_trace_payload(
        "https://private.invalid/image.png",
        key="image_url",
    ) == "<redacted>"


# ------------------------------------------------------------------ #
# creator public media contract                                     #
# ------------------------------------------------------------------ #

def test_playable_voice_uri_accepts_audio_data_and_rejects_opaque_schemes():
    from orchestrator.creators import _is_playable_voice_uri

    assert _is_playable_voice_uri("data:audio/wav;base64,AAAA") is True
    assert _is_playable_voice_uri("voice-id:opaque") is False


# ------------------------------------------------------------------ #
# graph review routing                                              #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("concepts", "concepts"),
        ("scripts", "scripts"),
        ("creators", "creator_profiles"),
    ],
)
async def test_review_regeneration_routes_to_the_requested_creative_stage(target, expected):
    from orchestrator.graph.builder import route_after_review

    assert await route_after_review(
        {"review_approved": False, "revision_request": {"target": target}}
    ) == expected


# ------------------------------------------------------------------ #
# video-agent take settlement                                       #
# ------------------------------------------------------------------ #

def test_settle_takes_records_paid_superseded_outputs():
    from orchestrator.nodes.stages import _settle_takes

    first = Artifact(kind="clip", uri="mock://take-1", meta={"cost_usd": 0.1})
    final = Artifact(kind="clip", uri="mock://take-2", meta={"cost_usd": 0.2})
    run = AgentRunResult(
        result=final,
        attempts=(
            ToolAttempt(
                call=ToolCall(
                    id="one",
                    name="generate_clip",
                    arguments={"revision": "fix hands"},
                ),
                result=first,
            ),
            ToolAttempt(
                call=ToolCall(id="two", name="generate_clip"),
                result=final,
            ),
        ),
    )

    settled, cost = _settle_takes(run)

    assert cost == 0.3
    assert settled.meta["agent_takes"] == 2
    assert settled.meta["superseded_takes"] == [
        {
            "uri": "mock://take-1",
            "cost_usd": 0.1,
            "revision": "fix hands",
        }
    ]


# ------------------------------------------------------------------ #
# stores: read corrompido + fallbacks                               #
# ------------------------------------------------------------------ #

def test_creator_store_load_returns_empty_on_corrupt_json(tmp_path):
    path = tmp_path / "creators.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert creator_store.load_creators(path) == []


def test_feedback_store_load_returns_none_on_corrupt_json(tmp_path):
    path = tmp_path / "fb.json"
    path.write_text("{broken", encoding="utf-8")
    assert feedback_store.load_feedback(path, "run-1") is None


def test_feedback_store_latest_none_when_no_valid_entries(tmp_path):
    import json

    path = tmp_path / "fb.json"
    # store não-vazio, mas sem nenhuma entrada com _idx inteiro válido
    path.write_text(json.dumps({"run-1": {"produced": 3}}), encoding="utf-8")
    assert feedback_store.load_latest_feedback(path) is None


def test_prompt_store_read_returns_empty_on_corrupt_json(tmp_path):
    path = tmp_path / "prompts.json"
    path.write_text("{oops", encoding="utf-8")
    assert prompt_store.list_templates(path) == []


def test_record_last_used_noop_without_prompts(tmp_path):
    path = tmp_path / "prompts.json"
    prompt_store.record_last_used(path, creator_prompt=None, video_prompt="   ")
    assert not path.exists()  # nada a gravar → early return, sem tocar o disco


# ------------------------------------------------------------------ #
# media_store                                                       #
# ------------------------------------------------------------------ #

def test_ext_from_mime_guesses_unknown_mime():
    assert media_store._ext_from_mime("text/html") in ("html", "htm")


def test_ext_from_mime_defaults_for_unguessable():
    assert media_store._ext_from_mime("application/x-nonsense") == media_store._DEFAULT_EXT


def test_ext_from_url_none_without_extension():
    assert media_store._ext_from_url("https://cdn.example/path/no-ext") is None


async def test_persist_item_media_requires_root():
    with pytest.raises(TypeError, match="videos_root"):
        await media_store.persist_item_media({"id": "x"}, run_id="r")


async def test_persist_media_downloads_http_with_own_client(monkeypatch, tmp_path):
    class _Resp:
        content = b"BYTES"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, uri):
            return _Resp()

        async def aclose(self):
            return None

    monkeypatch.setattr(media_store.httpx, "AsyncClient", lambda *a, **k: _Client())

    out = await media_store.persist_media(
        "https://cdn.example/pic", tmp_path, "image", web_prefix="/media/run/creator-0"
    )

    assert out == "/media/run/creator-0/image.png"
    assert (tmp_path / "image.png").read_bytes() == b"BYTES"
