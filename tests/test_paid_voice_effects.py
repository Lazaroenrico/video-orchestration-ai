from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from orchestrator.graph.state import Artifact
from orchestrator.tools.assembly import synthesize_voiceover_tool
from orchestrator.tools.base import ToolContext, execute_paid_effect
from orchestrator.tools.creators import (
    design_creator_voice_tool,
    finalize_creator_voice_tool,
)


class FakeLedger:
    def __init__(self) -> None:
        self.effects: dict[str, SimpleNamespace] = {}
        self.reservations: list[dict] = []

    async def reserve(self, effect_key, **kwargs):
        self.reservations.append({"effect_key": effect_key, **kwargs})
        if effect_key in self.effects:
            effect = self.effects[effect_key]
            replay = {**vars(effect), "created": False}
            return SimpleNamespace(**replay)
        effect = SimpleNamespace(status="reserved", result=None, created=True)
        self.effects[effect_key] = effect
        return effect

    async def mark_succeeded(self, effect_key, *, result):
        self.effects[effect_key].status = "succeeded"
        self.effects[effect_key].result = result

    async def mark_failed(self, effect_key, *, error, release_quota):
        effect = self.effects[effect_key]
        effect.status = "failed"
        effect.error = error
        effect.release_quota = release_quota

    async def mark_uncertain(self, effect_key, *, error):
        effect = self.effects[effect_key]
        effect.status = "uncertain"
        effect.error = error

    async def mark_reconciled(self, effect_key, *, result):
        effect = self.effects.setdefault(
            effect_key,
            SimpleNamespace(status="uncertain", result=None, created=False),
        )
        effect.status = "succeeded"
        effect.result = result


class VoiceAdapter:
    def __init__(self) -> None:
        self.design_calls = 0
        self.finalize_calls = 0
        self.tts_calls = 0

    async def design_voice_candidates(self, _spec, *, preview_text=None):
        self.design_calls += 1
        return {
            "provider": "elevenlabs",
            "design_model": "eleven_ttv_v3",
            "description_hash": "f2fe1d4b31",
            "prompt_version": "voice-match-v1",
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "preview": {
                        "kind": "voice_preview",
                        "uri": "data:audio/mpeg;base64,QUJD",
                    },
                    "duration_seconds": 4.0,
                    "media_type": "audio/mpeg",
                }
            ],
            "cost_usd": 0.01,
            "cost_source": "estimate",
        }

    async def finalize_voice(self, candidate_id, **_kwargs):
        self.finalize_calls += 1
        return {
            "provider": "elevenlabs",
            "voice_ref": "voice-permanent",
            "selected_candidate_id": candidate_id,
            "preview_uri": "r2://preview.mp3",
            "design_model": "eleven_ttv_v3",
            "tts_model": "eleven_turbo_v2_5",
        }

    async def reconcile_voice(self, candidate_id, **_kwargs):
        return {
            "provider": "elevenlabs",
            "voice_ref": "voice-reconciled",
            "selected_candidate_id": candidate_id,
            "preview_uri": "r2://preview.mp3",
            "design_model": "eleven_ttv_v3",
            "tts_model": "eleven_turbo_v2_5",
        }

    async def synthesize_voiceover(self, *, voice_ref, text):
        self.tts_calls += 1
        return Artifact(
            kind="voiceover",
            uri="data:audio/mpeg;base64,QUJD",
            meta={"voice_ref": voice_ref, "characters": len(text)},
        )


def _context(adapter, ledger) -> ToolContext:
    return ToolContext(
        adapter=adapter,
        pipeline={
            "voice": {
                "mode": "designed",
                "provider": "elevenlabs",
                "design_model": "eleven_ttv_v3",
                "tts_model": "eleven_turbo_v2_5",
            }
        },
        run={},
        run_id="run-1",
        effect_ledger=ledger,
        durable=True,
    )


def _spec() -> dict:
    return {
        "vocal_presentation": "neutral",
        "vocal_age": "adult",
        "timbre": "warm",
        "pace": "conversational",
        "energy": "balanced",
    }


async def test_paid_voice_effect_keys_quotas_and_completed_replay(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = VoiceAdapter()
    ledger = FakeLedger()
    ctx = _context(adapter, ledger)

    first = await design_creator_voice_tool(
        ctx,
        spec=_spec(),
        creator_id="creator-0",
        reroll_count=1,
    )
    replay = await design_creator_voice_tool(
        ctx,
        spec=_spec(),
        creator_id="creator-0",
        reroll_count=1,
    )
    await finalize_creator_voice_tool(
        ctx,
        candidate_id="candidate-1",
        batch=first,
        creator_id="creator-0",
    )
    await synthesize_voiceover_tool(
        ctx,
        item_id="item-1",
        voice_ref="voice-permanent",
        text="Approved narration",
    )

    assert replay == first
    assert adapter.design_calls == 1
    assert [entry["provider"] for entry in ledger.reservations] == [
        "elevenlabs_voice_design_chars",
        "elevenlabs_voice_design_chars",
        "elevenlabs_voice_slots",
        "elevenlabs_tts_chars",
    ]
    keys = [entry["effect_key"] for entry in ledger.reservations]
    assert keys[0].startswith("voice-design:run-1:creator-0:")
    assert keys[0].endswith(":1")
    assert keys[2] == "voice-finalize:run-1:creator-0:candidate-1"
    assert keys[3].startswith("voiceover:run-1:item-1:")
    assert keys[3].endswith(":voice-permanent")


async def test_durable_paid_effect_requires_opt_in_and_ledger(monkeypatch) -> None:
    adapter = VoiceAdapter()
    monkeypatch.delenv("ORCH_ENABLE_PAID_ADAPTERS", raising=False)
    with pytest.raises(RuntimeError, match="ORCH_ENABLE_PAID_ADAPTERS"):
        await design_creator_voice_tool(
            _context(adapter, FakeLedger()),
            spec=_spec(),
            creator_id="creator-0",
        )

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    with pytest.raises(RuntimeError, match="PostgresEffectLedger"):
        await design_creator_voice_tool(
            _context(adapter, None),
            spec=_spec(),
            creator_id="creator-0",
        )
    assert adapter.design_calls == 0


@pytest.mark.parametrize(
    ("error", "status", "released"),
    [
        (httpx.ConnectTimeout("pre-send"), "failed", True),
        (httpx.ReadTimeout("post-send"), "uncertain", None),
        (RuntimeError("malformed post-send response"), "uncertain", None),
    ],
)
async def test_paid_effect_failure_classification(
    monkeypatch,
    error,
    status,
    released,
) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    ledger = FakeLedger()
    ctx = _context(VoiceAdapter(), ledger)

    async def fail():
        raise error

    with pytest.raises(type(error)):
        await execute_paid_effect(
            ctx,
            effect_key="voice-design:run-1:creator-0:hash:0",
            provider="elevenlabs_voice_design_chars",
            units=100,
            request={"description_hash": "hash"},
            operation=fail,
        )

    effect = ledger.effects["voice-design:run-1:creator-0:hash:0"]
    assert effect.status == status
    assert getattr(effect, "release_quota", None) is released


async def test_non_durable_paid_effect_executes_without_ledger() -> None:
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return {"value": "local"}

    result = await execute_paid_effect(
        ToolContext(adapter=object(), pipeline={}, run={}, run_id="local"),
        effect_key="unused",
        provider="unused",
        units=1,
        request={},
        operation=operation,
    )

    assert result == {"value": "local"}
    assert calls == 1


async def test_paid_effect_propagates_non_uncertain_reservation_error(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")

    class BrokenLedger(FakeLedger):
        async def reserve(self, effect_key, **kwargs):
            raise RuntimeError("database unavailable")

    async def operation():
        raise AssertionError("operation must not run")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await execute_paid_effect(
            _context(VoiceAdapter(), BrokenLedger()),
            effect_key="effect",
            provider="provider",
            units=1,
            request={},
            operation=operation,
            reconcile=lambda: operation(),
        )


async def test_uncertain_reservation_exception_reconciles(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")

    class UncertainEffectError(RuntimeError):
        pass

    class UncertainLedger(FakeLedger):
        async def reserve(self, effect_key, **kwargs):
            raise UncertainEffectError("ambiguous prior reservation")

    ledger = UncertainLedger()

    async def operation():
        raise AssertionError("operation must not run")

    async def reconcile():
        return {"voice_ref": "voice-reconciled"}

    result = await execute_paid_effect(
        _context(VoiceAdapter(), ledger),
        effect_key="effect",
        provider="provider",
        units=1,
        request={},
        operation=operation,
        reconcile=reconcile,
    )

    assert result == {"voice_ref": "voice-reconciled"}
    assert ledger.effects["effect"].status == "succeeded"


@pytest.mark.parametrize("result", [None, {}])
async def test_succeeded_paid_effect_requires_non_empty_replay_result(
    monkeypatch, result,
) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    ledger = FakeLedger()
    ledger.effects["effect"] = SimpleNamespace(status="succeeded", result=result)

    with pytest.raises(RuntimeError, match="has no replay result"):
        await execute_paid_effect(
            _context(VoiceAdapter(), ledger),
            effect_key="effect",
            provider="provider",
            units=1,
            request={},
            operation=lambda: pytest.fail("operation must not run"),
        )


async def test_uncertain_paid_effect_reconciles_via_finalize_tool(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = VoiceAdapter()
    ledger = FakeLedger()
    key = "voice-finalize:run-1:creator-0:candidate-1"
    ledger.effects[key] = SimpleNamespace(status="uncertain", result=None)

    result = await finalize_creator_voice_tool(
        _context(adapter, ledger),
        candidate_id="candidate-1",
        batch={"description_hash": "hash", "candidates": []},
        creator_id="creator-0",
    )

    assert result["voice_ref"] == "voice-reconciled"
    assert adapter.finalize_calls == 0
    assert ledger.effects[key].status == "succeeded"


async def test_duplicate_reserved_paid_effect_becomes_uncertain(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    ledger = FakeLedger()
    ledger.effects["effect"] = SimpleNamespace(status="reserved", result=None)

    with pytest.raises(RuntimeError, match="is ambiguous"):
        await execute_paid_effect(
            _context(VoiceAdapter(), ledger),
            effect_key="effect",
            provider="provider",
            units=1,
            request={},
            operation=lambda: pytest.fail("operation must not run"),
        )

    assert ledger.effects["effect"].status == "uncertain"
