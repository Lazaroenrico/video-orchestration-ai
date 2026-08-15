from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters.mock import MockAdapter
from orchestrator.registry import CompositeAdapter
from orchestrator.tools.base import ToolContext
from orchestrator.tools.creators import build_creator_tool


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


class PaidImageCreatorAdapter:
    def __init__(self, *, fail_error: Exception | None = None) -> None:
        self.image = SimpleNamespace(model="openai/gpt-image-2")
        self.build_calls = 0
        self.fail_error = fail_error

    async def build_creator(
        self,
        index: int,
        system_prompt: str | None = None,
        voice_profile: VoiceProfile | None = None,
    ) -> dict[str, Any]:
        self.build_calls += 1
        if self.fail_error is not None:
            raise self.fail_error
        return {
            "id": f"creator-{index}",
            "angles": ["front", "3/4", "profile", "smile", "neutral"],
            "upscaled_base": "https://cdn.openai.com/face-0.png",
            "voice_id": "voice-123",
            "voice_profile": (
                {"preset": voice_profile.preset, "prompt": voice_profile.prompt}
                if voice_profile
                else None
            ),
        }


def _context(adapter: Any, ledger: Any, *, durable: bool = True) -> ToolContext:
    return ToolContext(
        adapter=adapter,
        pipeline={
            "creator": {"image_model": "openai/gpt-image-2"},
        },
        run={"organization_slug": "acme"},
        run_id="run-1",
        effect_ledger=ledger,
        durable=durable,
    )


async def test_paid_image_effect_keys_quotas_and_completed_replay(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter()
    ledger = FakeLedger()
    ctx = _context(adapter, ledger)

    prompt = "Criadora UGC jovem, estilo casual e autêntico."
    voice = VoiceProfile(preset="female", prompt="Tom caloroso e conversacional.")

    first = await build_creator_tool(
        ctx,
        index=0,
        system_prompt=prompt,
        voice_profile=voice,
    )
    replay = await build_creator_tool(
        ctx,
        index=0,
        system_prompt=prompt,
        voice_profile=voice,
    )

    assert replay == first
    assert adapter.build_calls == 1
    assert len(ledger.reservations) == 2
    assert all(entry["provider"] == "openai_image_units" for entry in ledger.reservations)
    assert all(entry["units"] == 1 for entry in ledger.reservations)

    prompt_hash = hashlib.sha256(
        prompt.encode("utf-8") + "female".encode("utf-8")
    ).hexdigest()[:16]
    expected_key = f"creator-image:run-1:creator-0:{prompt_hash}"
    assert ledger.reservations[0]["effect_key"] == expected_key
    assert ledger.reservations[1]["effect_key"] == expected_key

    req = ledger.reservations[0]["request"]
    assert req["creator_id"] == "creator-0"
    assert req["index"] == 0
    assert req["prompt_hash"] == prompt_hash
    assert req["gender"] == "female"
    assert req["model"] == "openai/gpt-image-2"


async def test_durable_paid_image_effect_requires_opt_in_and_ledger(monkeypatch) -> None:
    adapter = PaidImageCreatorAdapter()
    monkeypatch.delenv("ORCH_ENABLE_PAID_ADAPTERS", raising=False)
    with pytest.raises(RuntimeError, match="ORCH_ENABLE_PAID_ADAPTERS"):
        await build_creator_tool(
            _context(adapter, FakeLedger()),
            index=0,
            system_prompt="Test prompt",
        )

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    with pytest.raises(RuntimeError, match="PostgresEffectLedger"):
        await build_creator_tool(
            _context(adapter, None),
            index=0,
            system_prompt="Test prompt",
        )
    assert adapter.build_calls == 0


@pytest.mark.parametrize(
    ("error", "status", "released"),
    [
        (httpx.ConnectTimeout("pre-send"), "failed", True),
        (httpx.ReadTimeout("post-send"), "uncertain", None),
        (RuntimeError("malformed post-send response"), "uncertain", None),
    ],
)
async def test_paid_image_effect_failure_classification(
    monkeypatch,
    error,
    status,
    released,
) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter(fail_error=error)
    ledger = FakeLedger()
    ctx = _context(adapter, ledger)

    prompt = "Fail prompt"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    effect_key = f"creator-image:run-1:creator-0:{prompt_hash}"

    with pytest.raises(type(error)):
        await build_creator_tool(
            ctx,
            index=0,
            system_prompt=prompt,
        )

    assert effect_key in ledger.effects
    effect = ledger.effects[effect_key]
    assert effect.status == status
    assert getattr(effect, "release_quota", None) is released


async def test_non_durable_paid_image_effect_executes_without_ledger() -> None:
    adapter = PaidImageCreatorAdapter()
    ctx = _context(adapter, None, durable=False)

    result = await build_creator_tool(
        ctx,
        index=0,
        system_prompt="Local prompt",
    )

    assert result["id"] == "creator-0"
    assert adapter.build_calls == 1


async def test_mock_adapter_executes_without_ledger_or_quota(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    mock = MockAdapter(tiers=[{"name": "pruna", "model": "m", "cost_per_second": 0.01}])
    ledger = FakeLedger()
    ctx = _context(mock, ledger, durable=True)

    result = await build_creator_tool(
        ctx,
        index=0,
        system_prompt="Mock prompt",
    )

    assert result["id"] == "creator-0"
    assert len(ledger.reservations) == 0


@pytest.mark.parametrize("result", [None, {}])
async def test_succeeded_paid_image_effect_requires_non_empty_replay_result(
    monkeypatch, result,
) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter()
    ledger = FakeLedger()
    prompt = "Prompt"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    key = f"creator-image:run-1:creator-0:{prompt_hash}"
    ledger.effects[key] = SimpleNamespace(status="succeeded", result=result)

    with pytest.raises(RuntimeError, match="has no replay result"):
        await build_creator_tool(
            _context(adapter, ledger),
            index=0,
            system_prompt=prompt,
        )


async def test_duplicate_reserved_paid_image_effect_becomes_uncertain(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter()
    ledger = FakeLedger()
    prompt = "Prompt"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    key = f"creator-image:run-1:creator-0:{prompt_hash}"
    ledger.effects[key] = SimpleNamespace(status="reserved", result=None)

    with pytest.raises(RuntimeError, match="is ambiguous"):
        await build_creator_tool(
            _context(adapter, ledger),
            index=0,
            system_prompt=prompt,
        )

    assert ledger.effects[key].status == "uncertain"


async def test_composite_adapter_with_paid_creator_is_protected(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    paid_creator = PaidImageCreatorAdapter()
    composite = CompositeAdapter(
        by_role={
            "creator": paid_creator,
            "video": object(),
            "qc": object(),
            "assembly": object(),
            "upscale": object(),
        }
    )
    ledger = FakeLedger()
    ctx = _context(composite, ledger, durable=True)

    result = await build_creator_tool(
        ctx,
        index=1,
        system_prompt="Composite prompt",
    )

    assert result["id"] == "creator-1"
    assert paid_creator.build_calls == 1
    assert len(ledger.reservations) == 1
    assert ledger.reservations[0]["provider"] == "openai_image_units"
    assert ledger.reservations[0]["request"]["creator_id"] == "creator-1"
