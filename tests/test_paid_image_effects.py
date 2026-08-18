from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters.mock import MockAdapter
from orchestrator.registry import CompositeAdapter
from orchestrator.storage.base import StoredObject
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


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


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
            "upscaled_base": _PNG_DATA_URI,
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


async def test_paid_image_effect_keys_quotas_and_completed_replay(monkeypatch, tmp_path) -> None:
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
        media_root=tmp_path,
    )
    replay = await build_creator_tool(
        ctx,
        index=0,
        system_prompt=prompt,
        voice_profile=voice,
        media_root=tmp_path,
    )

    assert replay == first
    assert adapter.build_calls == 1
    assert len(ledger.reservations) == 2
    assert all(entry["provider"] == "openai_image_units" for entry in ledger.reservations)
    assert all(entry["units"] == 1 for entry in ledger.reservations)

    prompt_hash = hashlib.sha256(
        prompt.encode("utf-8") + "female".encode("utf-8")
    ).hexdigest()[:16]
    model_slug = "openai_gpt-image-2"
    expected_key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"
    assert ledger.reservations[0]["effect_key"] == expected_key
    assert ledger.reservations[1]["effect_key"] == expected_key

    req = ledger.reservations[0]["request"]
    assert req["creator_id"] == "creator-0"
    assert req["index"] == 0
    assert req["prompt_hash"] == prompt_hash
    assert req["gender"] == "female"
    assert req["model"] == "openai/gpt-image-2"

    # Canonical persistence in ledger result (ADR-D30 / D45 / D47)
    saved_result = ledger.effects[expected_key].result
    assert saved_result is not None
    assert saved_result["upscaled_base"] == "/media/run-1/creator-0/image.png"
    assert "image_source_uri" not in saved_result
    assert first["upscaled_base"] == "/media/run-1/creator-0/image.png"
    assert "image_source_uri" not in first


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
    model_slug = "openai_gpt-image-2"
    effect_key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"

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
    model_slug = "openai_gpt-image-2"
    key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"
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
    model_slug = "openai_gpt-image-2"
    key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"
    ledger.effects[key] = SimpleNamespace(status="reserved", result=None)

    with pytest.raises(RuntimeError, match="is ambiguous"):
        await build_creator_tool(
            _context(adapter, ledger),
            index=0,
            system_prompt=prompt,
        )

    assert ledger.effects[key].status == "uncertain"


async def test_replay_failed_paid_image_effect_becomes_uncertain(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter()
    ledger = FakeLedger()
    prompt = "Prompt com falha prévia"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    model_slug = "openai_gpt-image-2"
    key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"
    ledger.effects[key] = SimpleNamespace(status="failed", result=None)

    with pytest.raises(RuntimeError, match="is ambiguous"):
        await build_creator_tool(
            _context(adapter, ledger),
            index=0,
            system_prompt=prompt,
        )

    assert ledger.effects[key].status == "uncertain"
    assert adapter.build_calls == 0


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
    model_slug = "openai_gpt-image-2"
    assert model_slug in ledger.reservations[0]["effect_key"]


class FakeR2Storage:
    def __init__(self, *, fail_error: Exception | None = None, prefix: str = "r2://my-bucket") -> None:
        self.backend = "r2"
        self.fail_error = fail_error
        self.prefix = prefix
        self.puts: list[StoredObject] = []

    async def put_from_url(self, uri: str, *, key_base: str, client: Any = None) -> StoredObject | None:
        if self.fail_error is not None:
            raise self.fail_error
        from orchestrator.storage.base import fetch_media
        fetched = await fetch_media(uri, client=client)
        if fetched is None:
            return None
        key = f"{key_base}.{fetched.ext}"
        obj = StoredObject(
            backend=self.backend,
            key=key,
            uri=f"{self.prefix}/{key}",
            content_type=fetched.content_type,
            size_bytes=len(fetched.data),
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.puts.append(obj)
        return obj


async def test_paid_image_effect_persists_canonical_r2_pointer_and_records_db(monkeypatch, tmp_path) -> None:
    from orchestrator.storage.db import ArtifactDB

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter()
    ledger = FakeLedger()
    ctx = _context(adapter, ledger)
    storage = FakeR2Storage()
    db = ArtifactDB(tmp_path / "artifacts.sqlite")
    db.setup()

    prompt = "Prompt para teste R2"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    model_slug = "openai_gpt-image-2"
    effect_key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"

    result = await build_creator_tool(
        ctx,
        index=0,
        system_prompt=prompt,
        storage=storage,
        db=db,
    )

    expected_uri = "r2://my-bucket/run-1/creator-0/image.png"
    assert result["upscaled_base"] == expected_uri
    assert "image_source_uri" not in result

    # Ledger saved result contains canonical r2 pointer
    saved_result = ledger.effects[effect_key].result
    assert saved_result["upscaled_base"] == expected_uri
    assert "image_source_uri" not in saved_result

    # ArtifactDB has recorded the image artifact
    records = await db.by_run("run-1")
    assert len(records) == 1
    assert records[0].kind == "image"
    assert records[0].creator_id == "creator-0"
    assert records[0].storage_key == "run-1/creator-0/image.png"
    assert records[0].storage_backend == "r2"

    # Replay returns canonical pointer from ledger without calling adapter
    replay = await build_creator_tool(
        ctx,
        index=0,
        system_prompt=prompt,
        storage=storage,
        db=db,
    )
    assert replay == result
    assert adapter.build_calls == 1


async def test_paid_image_effect_persistence_failure_occurs_before_mark_succeeded(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter()
    ledger = FakeLedger()
    ctx = _context(adapter, ledger)
    storage = FakeR2Storage(fail_error=RuntimeError("R2 upload timeout"))

    prompt = "Prompt com falha de persistência"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    model_slug = "openai_gpt-image-2"
    effect_key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"

    with pytest.raises(RuntimeError, match="R2 upload timeout"):
        await build_creator_tool(
            ctx,
            index=0,
            system_prompt=prompt,
            storage=storage,
        )

    # Adapter was called, but effect was NOT marked succeeded
    assert adapter.build_calls == 1
    assert effect_key in ledger.effects
    effect = ledger.effects[effect_key]
    assert effect.status != "succeeded"
    assert effect.status == "uncertain"
    assert effect.result is None


async def test_paid_image_effect_silent_storage_failure_raises_and_marks_uncertain(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    adapter = PaidImageCreatorAdapter()
    ledger = FakeLedger()
    ctx = _context(adapter, ledger)

    class SilentFailureStorage:
        async def put_from_url(self, uri: str, *, key_base: str, client: Any = None) -> StoredObject | None:
            return None

    storage = SilentFailureStorage()
    prompt = "Prompt com falha silenciosa de storage"
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    model_slug = "openai_gpt-image-2"
    effect_key = f"creator-image:run-1:creator-0:{model_slug}:{prompt_hash}"

    with pytest.raises(
        RuntimeError,
        match="failed to persist creator image to canonical storage for creator creator-0",
    ):
        await build_creator_tool(
            ctx,
            index=0,
            system_prompt=prompt,
            storage=storage,
        )

    assert adapter.build_calls == 1
    assert effect_key in ledger.effects
    effect = ledger.effects[effect_key]
    assert effect.status != "succeeded"
    assert effect.status == "uncertain"
    assert effect.result is None


