from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.graph.state import Artifact
from orchestrator.tools.base import ToolContext
from orchestrator.tools.video import VideoEffectError, generate_clip_tool


class FakeVideoLedger:
    """Boundary fake: the webhook has already bound the ambiguous prediction."""

    def __init__(self) -> None:
        self.effect: SimpleNamespace | None = None
        self.reservations: list[dict] = []

    async def reserve(self, effect_key, **kwargs):
        self.reservations.append({"effect_key": effect_key, **kwargs})
        self.effect = SimpleNamespace(
            effect_key=effect_key,
            status="reserved",
            result=None,
            created=True,
            provider_operation_id=None,
            provider_status=None,
        )
        return self.effect

    async def mark_uncertain(self, effect_key, *, error, error_type=None):
        assert self.effect is not None and self.effect.effect_key == effect_key
        self.effect.status = "uncertain"
        self.effect.error = error
        self.effect.error_type = error_type
        return self.effect

    async def wait_for_provider_operation(self, effect_key, **_kwargs):
        assert self.effect is not None and self.effect.effect_key == effect_key
        # Simulates the signed Replicate webhook arriving after the POST response
        # was lost. It is the only safe source for the prediction id at this point.
        self.effect.provider_operation_id = "prediction-from-webhook"
        self.effect.provider_status = "processing"
        return self.effect

    async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
        assert self.effect is not None and self.effect.effect_key == effect_key
        self.effect.provider_operation_id = provider_operation_id
        self.effect.provider_status = provider_status
        return self.effect

    async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
        assert self.effect is not None and self.effect.effect_key == effect_key
        self.effect.provider_status = provider_status
        if error_type is not None:
            self.effect.error_type = error_type
        return self.effect

    async def mark_succeeded(self, effect_key, *, result):
        assert self.effect is not None and self.effect.effect_key == effect_key
        self.effect.status = "succeeded"
        self.effect.result = result
        return self.effect

    async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
        assert self.effect is not None and self.effect.effect_key == effect_key
        self.effect.status = "failed"
        self.effect.error = error
        self.effect.error_type = error_type
        self.effect.release_quota = release_quota
        return self.effect


class FakePredictions:
    def __init__(self) -> None:
        self.create_calls = 0
        self.get_calls = 0

    async def async_create(self, *, model, input, **params):
        self.create_calls += 1
        if self.create_calls == 1:
            raise httpx.ConnectError("pre-send connection failure")
        raise httpx.WriteTimeout("")

    async def async_get(self, prediction_id):
        self.get_calls += 1
        assert prediction_id == "prediction-from-webhook"
        return SimpleNamespace(
            id=prediction_id,
            status="succeeded",
            output="https://cdn.replicate.com/reconciled.mp4",
            error=None,
        )

    async def async_cancel(self, prediction_id):  # pragma: no cover - success path
        raise AssertionError(f"unexpected cancellation of {prediction_id}")


async def test_connect_error_then_write_timeout_reconciles_without_third_post(monkeypatch):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    predictions = FakePredictions()
    adapter = ReplicateVideoAdapter(
        tiers=[
            {
                "name": "pruna",
                "model": "prunaai/p-video",
                "cost_per_second": 0.04,
                "max_concurrency": 1,
            }
        ],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        max_retries=3,
        backoff_base=0,
        allow_mock_fallback=False,
    )
    ledger = FakeVideoLedger()
    prompt = "PRIVATE OFFER: serum X"
    reference = "https://signed.example/private-creator.png?secret=1"
    ctx = ToolContext(
        adapter=adapter,
        pipeline={
            "clip": {"timeout_ms": 100},
            "video": {"reconciliation_poll_seconds": 0},
        },
        run={"organization_slug": "acme"},
        run_id="run-video-1",
        effect_ledger=ledger,
        durable=True,
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="pruna",
        seconds=8,
        attempt=1,
        system_prompt=prompt,
        reference_image_uri=reference,
        stage="talking_head",
    )

    assert artifact.uri == "https://cdn.replicate.com/reconciled.mp4"
    assert artifact.meta["prediction_id"] == "prediction-from-webhook"
    assert predictions.create_calls == 2  # safe pre-send retry; never a third POST
    assert predictions.get_calls == 1
    assert ledger.effect is not None
    assert ledger.effect.status == "succeeded"
    assert ledger.effect.error_type == "WriteTimeout"
    reservation = ledger.reservations[0]
    assert reservation["provider"] == "replicate_video_seconds"
    assert reservation["units"] == 8
    assert reservation["effect_key"].startswith(
        "video:run-video-1:item-1:talking_head:1:"
    )
    serialized_request = repr(reservation["request"])
    assert prompt not in serialized_request
    assert reference not in serialized_request


def test_durable_models_expose_provider_operation_and_error_types():
    from orchestrator.db.models import EffectLedger, Job, Run

    assert {
        "provider_operation_id",
        "provider_status",
        "error_type",
    } <= set(EffectLedger.__table__.columns.keys())
    assert "error_type" in Job.__table__.columns
    assert "error_type" in Run.__table__.columns


def test_empty_exception_message_still_has_persistable_job_error():
    from orchestrator.worker import _job_error_fields

    assert _job_error_fields(httpx.WriteTimeout("")) == (
        "WriteTimeout",
        "WriteTimeout",
    )


def test_public_item_contract_keeps_structured_failure():
    from orchestrator.graph.state import FailureDetail, Item
    from orchestrator.web.server import _item_payload_from_result

    item = Item(
        id="item-1",
        concept={"id": "item-1"},
        error="video provider operation failed (prediction_timeout)",
        failure=FailureDetail(
            code="prediction_timeout",
            type="WriteTimeout",
            message="video provider operation failed (prediction_timeout)",
            stage="talking_head",
            provider="replicate",
            item_id="item-1",
            effect_key="video:run-1:item-1:talking_head:0:hash",
            retryable=False,
            uncertain=True,
        ),
    )

    payload = _item_payload_from_result(item)

    assert payload["failure"]["stage"] == "talking_head"
    assert payload["failure"]["type"] == "WriteTimeout"


async def test_crash_retry_polls_persisted_prediction_without_new_post(monkeypatch):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    create_calls = 0

    async def async_create(**_kwargs):
        nonlocal create_calls
        create_calls += 1
        raise AssertionError("retry must not create another prediction")

    async def async_get(prediction_id):
        return SimpleNamespace(
            id=prediction_id,
            status="succeeded",
            output="https://cdn.replicate.com/replayed.mp4",
            error=None,
        )

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=lambda _id: None,
    )
    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "pruna", "model": "prunaai/p-video", "cost_per_second": 0.04}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        backoff_base=0,
        allow_mock_fallback=False,
    )

    class CrashLedger(FakeVideoLedger):
        def __init__(self):
            super().__init__()
            self.effect = SimpleNamespace(
                effect_key="placeholder",
                status="reserved",
                result=None,
                created=False,
                provider_operation_id="prediction-persisted",
                provider_status="processing",
                error_type=None,
            )

        async def reserve(self, effect_key, **kwargs):
            self.reservations.append({"effect_key": effect_key, **kwargs})
            self.effect.effect_key = effect_key
            return self.effect

    ledger = CrashLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={},
        run_id="run-retry",
        effect_ledger=ledger,
        durable=True,
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="pruna",
        seconds=8,
        attempt=0,
        stage="talking_head",
    )

    assert artifact.uri.endswith("replayed.mp4")
    assert create_calls == 0


async def test_missing_webhook_until_deadline_stays_uncertain_without_repost(monkeypatch):
    from orchestrator.tools.video import VideoEffectError

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")
    create_calls = 0

    async def async_create(**_kwargs):
        nonlocal create_calls
        create_calls += 1
        raise httpx.WriteTimeout("")

    predictions = SimpleNamespace(async_create=async_create)
    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "pruna", "model": "prunaai/p-video", "cost_per_second": 0.04}],
        prediction_client=SimpleNamespace(models=SimpleNamespace(predictions=predictions)),
        backoff_base=0,
        allow_mock_fallback=False,
    )
    class NoWebhookLedger(FakeVideoLedger):
        async def wait_for_provider_operation(self, effect_key, **_kwargs):
            assert self.effect is not None and self.effect.effect_key == effect_key
            return self.effect

    ledger = NoWebhookLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 1}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-timeout",
        effect_ledger=ledger,
        durable=True,
    )

    with pytest.raises(VideoEffectError) as error:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="pruna",
            seconds=8,
            attempt=0,
            stage="talking_head",
        )

    assert create_calls == 1
    assert error.value.uncertain is True
    assert error.value.error_type == "WriteTimeout"
    assert ledger.effect is not None and ledger.effect.status == "uncertain"


@pytest.mark.parametrize("provider_status", ["failed", "canceled"])
async def test_terminal_provider_failure_is_definitive(monkeypatch, provider_status):
    from orchestrator.tools.video import VideoEffectError

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    async def async_create(**_kwargs):
        return SimpleNamespace(id="prediction-terminal", status="starting", output=None, error=None)

    async def async_get(_prediction_id):
        return SimpleNamespace(
            id="prediction-terminal",
            status=provider_status,
            output=None,
            error="provider rejected prediction",
        )

    predictions = SimpleNamespace(async_create=async_create, async_get=async_get)
    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "pruna", "model": "prunaai/p-video", "cost_per_second": 0.04}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        backoff_base=0,
        allow_mock_fallback=False,
    )
    ledger = FakeVideoLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-terminal",
        effect_ledger=ledger,
        durable=True,
    )

    with pytest.raises(VideoEffectError) as error:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="pruna",
            seconds=8,
            attempt=0,
        )

    assert error.value.uncertain is False
    assert error.value.code == f"prediction_{provider_status}"
    assert ledger.effect is not None and ledger.effect.status == "failed"


async def test_known_prediction_is_canceled_at_deadline_and_becomes_definitive(monkeypatch):
    from orchestrator.tools.video import VideoEffectError

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")
    cancel_calls = 0

    async def async_create(**_kwargs):
        return SimpleNamespace(id="prediction-slow", status="starting", output=None, error=None)

    async def async_get(_prediction_id):
        return SimpleNamespace(id="prediction-slow", status="processing", output=None, error=None)

    async def async_cancel(_prediction_id):
        nonlocal cancel_calls
        cancel_calls += 1
        return SimpleNamespace(id="prediction-slow", status="canceled", output=None, error=None)

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=async_cancel,
    )
    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "pruna", "model": "prunaai/p-video", "cost_per_second": 0.04}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        backoff_base=0,
        allow_mock_fallback=False,
    )
    ledger = FakeVideoLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 1}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-slow",
        effect_ledger=ledger,
        durable=True,
    )

    with pytest.raises(VideoEffectError) as error:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="pruna",
            seconds=8,
            attempt=0,
        )

    assert error.value.code == "prediction_canceled"
    assert error.value.uncertain is False
    assert cancel_calls == 1
    assert ledger.effect is not None and ledger.effect.status == "failed"


async def test_durable_latentsync_executes_both_stages_with_distinct_effect_keys(monkeypatch):
    """Pipeline durável de 2 estágios cria reservas para vídeo base e LatentSync."""
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    create_calls: list[dict] = []

    async def async_create(*, model=None, version=None, input=None, **params):
        create_calls.append({"model": model, "version": version, "input": input, "params": params})
        if model == "lightricks/ltx-2.3-fast":
            return SimpleNamespace(id="pred-ltx-1", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if model == "bytedance/latentsync" or version == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293":
            return SimpleNamespace(id="pred-ls-1", status="succeeded", output="https://cdn.replicate.com/latentsync.mp4", error=None)
        raise ValueError(f"unknown model/version {model}/{version}")

    async def async_get(prediction_id):
        if prediction_id == "pred-ltx-1":
            return SimpleNamespace(id="pred-ltx-1", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if prediction_id == "pred-ls-1":
            return SimpleNamespace(id="pred-ls-1", status="succeeded", output="https://cdn.replicate.com/latentsync.mp4", error=None)
        raise ValueError(f"unknown prediction {prediction_id}")

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=lambda _id: None,
    )

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync={
            "enabled": True,
            "model": "bytedance/latentsync",
            "resolution": "720p",
            "max_retries": 3,
            "required": True,
            "cost_per_second": 0.003,
        },
        allow_mock_fallback=False,
    )

    class MultiEffectLedger:
        def __init__(self):
            self.effects: dict[str, SimpleNamespace] = {}
            self.reservations: list[dict] = []

        async def reserve(self, effect_key, **kwargs):
            self.reservations.append({"effect_key": effect_key, **kwargs})
            effect = SimpleNamespace(
                effect_key=effect_key,
                status="reserved",
                result=None,
                created=True,
                provider_operation_id=None,
                provider_status=None,
            )
            self.effects[effect_key] = effect
            return effect

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            effect = self.effects[effect_key]
            effect.provider_operation_id = provider_operation_id
            effect.provider_status = provider_status
            return effect

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            effect = self.effects[effect_key]
            effect.provider_status = provider_status
            if error_type:
                effect.error_type = error_type
            return effect

        async def mark_succeeded(self, effect_key, *, result):
            effect = self.effects[effect_key]
            effect.status = "succeeded"
            effect.result = result
            return effect

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "failed"
            effect.error = error
            effect.error_type = error_type
            effect.release_quota = release_quota
            return effect

        async def mark_uncertain(self, effect_key, *, error, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "uncertain"
            effect.error = error
            effect.error_type = error_type
            return effect

        async def wait_for_provider_operation(self, effect_key, **_kwargs):
            return self.effects[effect_key]

        async def get(self, effect_key):
            return self.effects[effect_key]

    ledger = MultiEffectLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-ls-1",
        effect_ledger=ledger,
        durable=True,
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="PRIVATE PROMPT",
        reference_image_uri="https://cdn.r2.com/face.png",
        audio_uri="https://cdn.r2.com/voice.wav",
        stage="talking_head",
    )

    assert artifact.uri == "https://cdn.replicate.com/latentsync.mp4"
    assert artifact.meta["latentsync_applied"] is True
    assert artifact.meta["latentsync_model"] == "bytedance/latentsync"
    assert artifact.meta["prediction_id"] == "pred-ls-1"
    assert artifact.meta["base_clip_uri"] == "https://cdn.replicate.com/ltx.mp4"
    assert len(ledger.reservations) == 2
    assert ledger.reservations[0]["effect_key"].startswith("video:run-ls-1:item-1:talking_head:1:")
    assert ledger.reservations[1]["effect_key"].startswith("latentsync:run-ls-1:item-1:talking_head:1:")
    assert len(create_calls) == 2
    assert create_calls[0]["model"] == "lightricks/ltx-2.3-fast"
    assert create_calls[1]["version"] == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293"
    assert create_calls[1]["input"]["video"] == "https://cdn.replicate.com/ltx.mp4"
    assert create_calls[1]["input"]["audio"] == "https://cdn.r2.com/voice.wav"


async def test_durable_latentsync_replays_completed_ltx_stage_and_only_runs_latentsync(monkeypatch):
    """Se o estágio 1 (LTX) já estiver concluído no ledger, apenas o LatentSync é executado."""
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    create_calls: list[dict] = []

    async def async_create(*, model=None, version=None, input=None, **params):
        create_calls.append({"model": model, "version": version, "input": input, "params": params})
        if model == "bytedance/latentsync" or version == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293":
            return SimpleNamespace(id="pred-ls-cached", status="succeeded", output="https://cdn.replicate.com/latentsync_final.mp4", error=None)
        raise AssertionError(f"LTX stage must not be recreated: {model}/{version}")

    async def async_get(prediction_id):
        if prediction_id == "pred-ls-cached":
            return SimpleNamespace(id="pred-ls-cached", status="succeeded", output="https://cdn.replicate.com/latentsync_final.mp4", error=None)
        raise ValueError(f"unknown prediction {prediction_id}")

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=lambda _id: None,
    )

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync={
            "enabled": True,
            "model": "bytedance/latentsync",
            "resolution": "720p",
            "max_retries": 3,
        },
        allow_mock_fallback=False,
    )

    class ReplayLTXLedger:
        def __init__(self):
            self.effects: dict[str, SimpleNamespace] = {}
            self.reservations: list[dict] = []

        async def reserve(self, effect_key, **kwargs):
            self.reservations.append({"effect_key": effect_key, **kwargs})
            if effect_key.startswith("video:"):
                effect = SimpleNamespace(
                    effect_key=effect_key,
                    status="succeeded",
                    result={
                        "provider_prediction_id": "pred-ltx-cached",
                        "artifact": {
                            "kind": "clip",
                            "uri": "https://cdn.replicate.com/ltx_cached.mp4",
                            "meta": {"tier": "ltx", "model": "lightricks/ltx-2.3-fast", "seconds": 8},
                        },
                    },
                    created=False,
                    provider_operation_id="pred-ltx-cached",
                    provider_status="succeeded",
                )
            else:
                effect = SimpleNamespace(
                    effect_key=effect_key,
                    status="reserved",
                    result=None,
                    created=True,
                    provider_operation_id=None,
                    provider_status=None,
                )
            self.effects[effect_key] = effect
            return effect

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            effect = self.effects[effect_key]
            effect.provider_operation_id = provider_operation_id
            effect.provider_status = provider_status
            return effect

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            effect = self.effects[effect_key]
            effect.provider_status = provider_status
            return effect

        async def mark_succeeded(self, effect_key, *, result):
            effect = self.effects[effect_key]
            effect.status = "succeeded"
            effect.result = result
            return effect

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "failed"
            effect.error = error
            return effect

        async def mark_uncertain(self, effect_key, *, error, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "uncertain"
            return effect

        async def wait_for_provider_operation(self, effect_key, **_kwargs):
            return self.effects[effect_key]

        async def get(self, effect_key):
            return self.effects[effect_key]

    ledger = ReplayLTXLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-ls-replay",
        effect_ledger=ledger,
        durable=True,
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        reference_image_uri="https://cdn.r2.com/face.png",
        audio_uri="https://cdn.r2.com/voice.wav",
        stage="talking_head",
    )

    assert artifact.uri == "https://cdn.replicate.com/latentsync_final.mp4"
    assert len(create_calls) == 1
    assert create_calls[0]["version"] == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293"
    assert create_calls[0]["input"]["video"] == "https://cdn.replicate.com/ltx_cached.mp4"


async def test_durable_latentsync_write_timeout_reconciles_via_webhook_without_third_post(monkeypatch):
    """WriteTimeout no LatentSync reconcilia por webhook/polling sem emitir novo POST."""
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    create_calls: list[dict] = []

    async def async_create(*, model=None, version=None, input=None, **params):
        create_calls.append({"model": model, "version": version, "input": input, "params": params})
        if model == "lightricks/ltx-2.3-fast":
            return SimpleNamespace(id="pred-ltx-ok", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if model == "bytedance/latentsync" or version == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293":
            if len([c for c in create_calls if c.get("version") == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293" or c.get("model") == "bytedance/latentsync"]) == 1:
                raise httpx.ConnectError("pre-send failure")
            raise httpx.WriteTimeout("")
        raise ValueError(f"unexpected model/version {model}/{version}")

    async def async_get(prediction_id):
        if prediction_id == "pred-ltx-ok":
            return SimpleNamespace(id="pred-ltx-ok", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if prediction_id == "pred-ls-webhook":
            return SimpleNamespace(id="pred-ls-webhook", status="succeeded", output="https://cdn.replicate.com/ls-reconciled.mp4", error=None)
        raise ValueError(f"unknown prediction {prediction_id}")

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=lambda _id: None,
    )

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync={
            "enabled": True,
            "model": "bytedance/latentsync",
            "resolution": "720p",
            "max_retries": 3,
        },
        backoff_base=0,
        allow_mock_fallback=False,
    )

    class WebhookLedger:
        def __init__(self):
            self.effects: dict[str, SimpleNamespace] = {}
            self.reservations: list[dict] = []

        async def reserve(self, effect_key, **kwargs):
            self.reservations.append({"effect_key": effect_key, **kwargs})
            effect = SimpleNamespace(
                effect_key=effect_key,
                status="reserved",
                result=None,
                created=True,
                provider_operation_id=None,
                provider_status=None,
            )
            self.effects[effect_key] = effect
            return effect

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            effect = self.effects[effect_key]
            effect.provider_operation_id = provider_operation_id
            effect.provider_status = provider_status
            return effect

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            effect = self.effects[effect_key]
            effect.provider_status = provider_status
            if error_type:
                effect.error_type = error_type
            return effect

        async def mark_succeeded(self, effect_key, *, result):
            effect = self.effects[effect_key]
            effect.status = "succeeded"
            effect.result = result
            return effect

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "failed"
            effect.error = error
            effect.error_type = error_type
            effect.release_quota = release_quota
            return effect

        async def mark_uncertain(self, effect_key, *, error, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "uncertain"
            effect.error = error
            effect.error_type = error_type
            return effect

        async def wait_for_provider_operation(self, effect_key, **_kwargs):
            effect = self.effects[effect_key]
            effect.provider_operation_id = "pred-ls-webhook"
            effect.provider_status = "processing"
            return effect

        async def get(self, effect_key):
            return self.effects[effect_key]

    ledger = WebhookLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-ls-timeout",
        effect_ledger=ledger,
        durable=True,
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        reference_image_uri="https://cdn.r2.com/face.png",
        audio_uri="https://cdn.r2.com/voice.wav",
        stage="talking_head",
    )

    assert artifact.uri == "https://cdn.replicate.com/ls-reconciled.mp4"
    ls_create_calls = [
        c for c in create_calls
        if c.get("version") == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293"
        or c.get("model") == "bytedance/latentsync"
    ]
    assert len(ls_create_calls) == 2  # 1 connect error retry + 1 write timeout (no third POST)


async def test_durable_latentsync_timeout_cancels_prediction_and_marks_failed(monkeypatch):
    """Timeout no LatentSync cancela predição no Replicate e marca efeito como falho/incerto."""
    from orchestrator.tools.video import VideoEffectError

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    cancel_calls: list[str] = []

    async def async_create(*, model=None, version=None, input=None, **params):
        if model == "lightricks/ltx-2.3-fast":
            return SimpleNamespace(id="pred-ltx-ok", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if model == "bytedance/latentsync" or version == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293":
            return SimpleNamespace(id="pred-ls-slow", status="starting", output=None, error=None)
        raise ValueError(f"unexpected model/version {model}/{version}")

    async def async_get(prediction_id):
        if prediction_id == "pred-ltx-ok":
            return SimpleNamespace(id="pred-ltx-ok", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if prediction_id == "pred-ls-slow":
            return SimpleNamespace(id="pred-ls-slow", status="processing", output=None, error=None)
        raise ValueError(f"unknown prediction {prediction_id}")

    async def async_cancel(prediction_id):
        cancel_calls.append(prediction_id)
        return SimpleNamespace(id=prediction_id, status="canceled", output=None, error=None)

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=async_cancel,
    )

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24, "timeout_ms": 1},
        latentsync={
            "enabled": True,
            "model": "bytedance/latentsync",
            "resolution": "720p",
            "max_retries": 3,
        },
        backoff_base=0,
        allow_mock_fallback=False,
    )

    class MultiEffectLedger:
        def __init__(self):
            self.effects: dict[str, SimpleNamespace] = {}

        async def reserve(self, effect_key, **kwargs):
            effect = SimpleNamespace(
                effect_key=effect_key,
                status="reserved",
                result=None,
                created=True,
                provider_operation_id=None,
                provider_status=None,
            )
            self.effects[effect_key] = effect
            return effect

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            effect = self.effects[effect_key]
            effect.provider_operation_id = provider_operation_id
            effect.provider_status = provider_status
            return effect

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            effect = self.effects[effect_key]
            effect.provider_status = provider_status
            if error_type:
                effect.error_type = error_type
            return effect

        async def mark_succeeded(self, effect_key, *, result):
            effect = self.effects[effect_key]
            effect.status = "succeeded"
            effect.result = result
            return effect

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "failed"
            effect.error = error
            effect.error_type = error_type
            effect.release_quota = release_quota
            return effect

        async def mark_uncertain(self, effect_key, *, error, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "uncertain"
            effect.error = error
            effect.error_type = error_type
            return effect

        async def wait_for_provider_operation(self, effect_key, **_kwargs):
            return self.effects[effect_key]

        async def get(self, effect_key):
            return self.effects[effect_key]

    ledger = MultiEffectLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 1}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-ls-slow",
        effect_ledger=ledger,
        durable=True,
    )

    with pytest.raises(VideoEffectError) as exc_info:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="ltx",
            seconds=8,
            attempt=1,
            reference_image_uri="https://cdn.r2.com/face.png",
            audio_uri="https://cdn.r2.com/voice.wav",
            stage="talking_head",
        )

    assert exc_info.value.code == "prediction_canceled"
    assert exc_info.value.effect_key.startswith("latentsync:run-ls-slow:item-1:talking_head:1:")
    assert cancel_calls == ["pred-ls-slow"]


async def test_durable_latentsync_provider_failure_raises_video_effect_error_without_fallback(monkeypatch):
    """Falha definitiva do LatentSync lança VideoEffectError sem fallback silencioso para clipe mudo."""
    from orchestrator.tools.video import VideoEffectError

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    async def async_create(*, model=None, version=None, input=None, **params):
        if model == "lightricks/ltx-2.3-fast":
            return SimpleNamespace(id="pred-ltx-ok", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if model == "bytedance/latentsync" or version == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293":
            return SimpleNamespace(id="pred-ls-failed", status="starting", output=None, error=None)
        raise ValueError(f"unexpected model/version {model}/{version}")

    async def async_get(prediction_id):
        if prediction_id == "pred-ltx-ok":
            return SimpleNamespace(id="pred-ltx-ok", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if prediction_id == "pred-ls-failed":
            return SimpleNamespace(id="pred-ls-failed", status="failed", output=None, error="CUDA out of memory")
        raise ValueError(f"unknown prediction {prediction_id}")

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=lambda _id: None,
    )

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync={
            "enabled": True,
            "model": "bytedance/latentsync",
            "resolution": "720p",
            "max_retries": 3,
        },
        backoff_base=0,
        allow_mock_fallback=False,
    )

    class MultiEffectLedger:
        def __init__(self):
            self.effects: dict[str, SimpleNamespace] = {}

        async def reserve(self, effect_key, **kwargs):
            effect = SimpleNamespace(
                effect_key=effect_key,
                status="reserved",
                result=None,
                created=True,
                provider_operation_id=None,
                provider_status=None,
            )
            self.effects[effect_key] = effect
            return effect

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            effect = self.effects[effect_key]
            effect.provider_operation_id = provider_operation_id
            effect.provider_status = provider_status
            return effect

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            effect = self.effects[effect_key]
            effect.provider_status = provider_status
            return effect

        async def mark_succeeded(self, effect_key, *, result):
            effect = self.effects[effect_key]
            effect.status = "succeeded"
            effect.result = result
            return effect

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "failed"
            effect.error = error
            effect.error_type = error_type
            effect.release_quota = release_quota
            return effect

        async def mark_uncertain(self, effect_key, *, error, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "uncertain"
            return effect

        async def wait_for_provider_operation(self, effect_key, **_kwargs):
            return self.effects[effect_key]

        async def get(self, effect_key):
            return self.effects[effect_key]

    ledger = MultiEffectLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-ls-fail",
        effect_ledger=ledger,
        durable=True,
    )

    with pytest.raises(VideoEffectError) as exc_info:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="ltx",
            seconds=8,
            attempt=1,
            reference_image_uri="https://cdn.r2.com/face.png",
            audio_uri="https://cdn.r2.com/voice.wav",
            stage="talking_head",
        )

    assert exc_info.value.code == "prediction_failed"
    assert exc_info.value.uncertain is False
    assert exc_info.value.effect_key.startswith("latentsync:run-ls-fail:item-1:talking_head:1:")


async def test_durable_latentsync_required_raises_video_effect_error_if_audio_missing(monkeypatch):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=SimpleNamespace(async_create=lambda **_: None)),
            predictions=SimpleNamespace(async_create=lambda **_: None, async_get=lambda _: None, async_cancel=lambda _: None),
        ),
        clip={"resolution": "720p"},
        latentsync={"enabled": True, "required": True},
        allow_mock_fallback=False,
    )

    class DummyLedger:
        async def get(self, effect_key):
            return None
        async def reserve(self, **kwargs):
            return SimpleNamespace(status="reserved")
        async def mark_failed(self, *args, **kwargs):
            pass

    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}},
        run={"organization_slug": "acme"},
        run_id="run-ls-req",
        effect_ledger=DummyLedger(),
        durable=True,
    )

    with pytest.raises(VideoEffectError) as exc_info:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="ltx",
            seconds=8,
            attempt=1,
            reference_image_uri="https://cdn.r2.com/face.png",
            audio_uri=None,
            stage="talking_head",
        )

    assert exc_info.value.error_type == "LatentSyncRequiredError"
    assert exc_info.value.code == "latentsync_audio_missing"


async def test_durable_latentsync_required_raises_video_effect_error_if_latentsync_disabled(monkeypatch):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=SimpleNamespace(async_create=lambda **_: None)),
            predictions=SimpleNamespace(async_create=lambda **_: None, async_get=lambda _: None, async_cancel=lambda _: None),
        ),
        clip={"resolution": "720p"},
        latentsync={"enabled": False, "required": True},
        allow_mock_fallback=False,
    )

    class DummyLedger:
        async def get(self, effect_key):
            return None
        async def reserve(self, **kwargs):
            return SimpleNamespace(status="reserved")
        async def mark_failed(self, *args, **kwargs):
            pass

    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}},
        run={"organization_slug": "acme"},
        run_id="run-ls-req-dis",
        effect_ledger=DummyLedger(),
        durable=True,
    )

    with pytest.raises(VideoEffectError) as exc_info:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="ltx",
            seconds=8,
            attempt=1,
            reference_image_uri="https://cdn.r2.com/face.png",
            audio_uri="https://cdn.r2.com/audio.wav",
            stage="talking_head",
        )

    assert exc_info.value.error_type == "LatentSyncRequiredError"
    assert exc_info.value.code == "latentsync_disabled"


async def test_durable_latentsync_persists_base_video_to_storage_before_completing_effect(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")

    mp4_b64 = "data:video/mp4;base64," + base64.b64encode(b"\x00mp4video").decode()

    class FakeReplicateClient:
        def __init__(self):
            self.predictions = self
            self.models = SimpleNamespace(predictions=self)

        async def async_create(self, **kwargs):
            return SimpleNamespace(id="pred-123", status="succeeded", output=mp4_b64)

        async def async_get(self, pred_id):
            return SimpleNamespace(id=pred_id, status="succeeded", output=mp4_b64)

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=FakeReplicateClient(),
        clip={"resolution": "720p"},
        latentsync={"enabled": True, "required": True, "model": "bytedance/latentsync"},
        allow_mock_fallback=False,
    )

    from orchestrator.storage.db import ArtifactDB
    db = ArtifactDB(tmp_path / "artifacts.sqlite")
    db.setup()

    class MultiEffectLedger:
        def __init__(self):
            self.effects: dict[str, SimpleNamespace] = {}
            self.reservations: list[dict] = []

        async def reserve(self, effect_key, **kwargs):
            self.reservations.append({"effect_key": effect_key, **kwargs})
            effect = SimpleNamespace(
                effect_key=effect_key,
                status="reserved",
                result=None,
                created=True,
                provider_operation_id=None,
                provider_status=None,
            )
            self.effects[effect_key] = effect
            return effect

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            effect = self.effects[effect_key]
            effect.provider_operation_id = provider_operation_id
            effect.provider_status = provider_status
            return effect

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            effect = self.effects[effect_key]
            effect.provider_status = provider_status
            if error_type:
                effect.error_type = error_type
            return effect

        async def mark_succeeded(self, effect_key, *, result):
            effect = self.effects[effect_key]
            effect.status = "succeeded"
            effect.result = result
            return effect

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "failed"
            effect.error = error
            effect.error_type = error_type
            effect.release_quota = release_quota
            return effect

    ledger = MultiEffectLedger()

    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 1000}},
        run={"organization_slug": "acme"},
        run_id="run-storage-test",
        effect_ledger=ledger,
        durable=True,
        videos_root=tmp_path,
        artifact_db=db,
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=5,
        attempt=1,
        reference_image_uri="https://cdn.r2.com/face.png",
        audio_uri="https://cdn.r2.com/audio.wav",
        stage="talking_head",
    )

    # 1. Returned artifact must have canonical storage URI and canonical base_clip_uri
    assert artifact.uri == "/videos/run-storage-test/items/item-1/clip-1.mp4"
    assert artifact.meta["base_clip_uri"] == "/videos/run-storage-test/items/item-1/base-clip-1.mp4"

    # 2. Files must exist on disk
    assert (tmp_path / "run-storage-test/items/item-1/base-clip-1.mp4").is_file()
    assert (tmp_path / "run-storage-test/items/item-1/clip-1.mp4").is_file()

    # 3. ArtifactDB must have both records
    rows = await db.by_run("run-storage-test")
    assert len(rows) == 2
    kinds = {r.kind: r for r in rows}
    assert "base_clip" in kinds
    assert "clip" in kinds
    assert kinds["base_clip"].storage_key == "run-storage-test/items/item-1/base-clip-1.mp4"
    assert kinds["clip"].storage_key == "run-storage-test/items/item-1/clip-1.mp4"

    # 4. Effect ledger entries must store the CANONICAL artifacts, not raw remote URLs
    video_effect = next(e for k, e in ledger.effects.items() if k.startswith("video:run-storage-test"))
    assert video_effect.status == "succeeded"
    assert video_effect.result["artifact"]["uri"] == "/videos/run-storage-test/items/item-1/base-clip-1.mp4"
    assert video_effect.result["artifact"]["meta"]["source_uri"] == mp4_b64

    ls_effect = next(e for k, e in ledger.effects.items() if k.startswith("latentsync:run-storage-test"))
    assert ls_effect.status == "succeeded"
    assert ls_effect.result["artifact"]["uri"] == "/videos/run-storage-test/items/item-1/clip-1.mp4"
    assert ls_effect.result["artifact"]["meta"]["base_clip_uri"] == "/videos/run-storage-test/items/item-1/base-clip-1.mp4"


async def test_durable_latentsync_replay_resolves_signed_url_instead_of_expired_source_uri(monkeypatch):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")

    latentsync_submitted_video_url = None

    async def async_create(**kwargs):
        nonlocal latentsync_submitted_video_url
        input_data = kwargs.get("input") or {}
        latentsync_submitted_video_url = input_data.get("video")
        return SimpleNamespace(id="pred-ls-fresh", status="succeeded", output="https://cdn.replicate.com/ls_out.mp4")

    async def async_get(pred_id):
        return SimpleNamespace(id=pred_id, status="succeeded", output="https://cdn.replicate.com/ls_out.mp4")

    class FakeClient:
        def __init__(self):
            self.predictions = self
            self.models = SimpleNamespace(predictions=self)
            self.async_create = async_create
            self.async_get = async_get

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=FakeClient(),
        clip={"resolution": "720p"},
        latentsync={"enabled": True, "required": True, "model": "bytedance/latentsync"},
        allow_mock_fallback=False,
    )

    class ReplayLedger:
        def __init__(self):
            self.effects = {
                "video:run-replay:item-1:talking_head:1:c63c377317ed22291929a666d65a9f784f4885ad24d139fdb63b509244c2b50f": SimpleNamespace(
                    status="succeeded",
                    result={
                        "artifact": {
                            "kind": "base_clip",
                            "uri": "r2://my-bucket/run-replay/items/item-1/base-clip-1.mp4",
                            "meta": {
                                "source_uri": "https://replicate.delivery/expired_1h_ago.mp4",
                                "storage_key": "run-replay/items/item-1/base-clip-1.mp4",
                                "storage_backend": "r2",
                            },
                        }
                    },
                )
            }

        async def reserve(self, effect_key, **kwargs):
            if effect_key in self.effects:
                return self.effects[effect_key]
            eff = SimpleNamespace(
                effect_key=effect_key,
                status="reserved",
                result=None,
                created=True,
                provider_operation_id=None,
                provider_status=None,
            )
            self.effects[effect_key] = eff
            return eff

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            eff = self.effects[effect_key]
            eff.provider_operation_id = provider_operation_id
            eff.provider_status = provider_status
            return eff

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            eff = self.effects[effect_key]
            eff.provider_status = provider_status
            return eff

        async def mark_succeeded(self, effect_key, *, result):
            eff = self.effects[effect_key]
            eff.status = "succeeded"
            eff.result = result
            return eff

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            eff = self.effects[effect_key]
            eff.status = "failed"
            return eff

    class FakeStorageResolver:
        async def get_signed_url(self, key: str) -> str:
            return f"https://r2.signed.com/{key}?signature=valid_signature_now"

    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 1000}},
        run={"organization_slug": "acme"},
        run_id="run-replay",
        effect_ledger=ReplayLedger(),
        durable=True,
        storage_resolver=FakeStorageResolver(),
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=5,
        attempt=1,
        reference_image_uri="https://cdn.r2.com/face.png",
        audio_uri="https://cdn.r2.com/audio.wav",
        stage="talking_head",
    )

    # LatentSync MUST have received the fresh signed URL, NOT the expired replicate.delivery URL
    assert latentsync_submitted_video_url == "https://r2.signed.com/run-replay/items/item-1/base-clip-1.mp4?signature=valid_signature_now"
    assert artifact.meta["base_clip_uri"] == "r2://my-bucket/run-replay/items/item-1/base-clip-1.mp4"


async def test_replicate_422_rejection_captures_error_detail(monkeypatch):
    from replicate.exceptions import ReplicateError

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")

    async def async_create(**_kwargs):
        raise ReplicateError(
            status=422,
            detail="ValidationError: resolution must be one of ['1080p', '2k', '4k']",
        )

    predictions = SimpleNamespace(async_create=async_create)
    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        backoff_base=0,
        allow_mock_fallback=False,
    )
    ledger = FakeVideoLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-422",
        effect_ledger=ledger,
        durable=True,
    )

    with pytest.raises(VideoEffectError) as exc_info:
        await generate_clip_tool(
            ctx,
            item_id="item-1",
            tier="ltx",
            seconds=8,
            attempt=1,
            stage="talking_head",
        )

    assert exc_info.value.code == "provider_rejected"
    assert exc_info.value.retryable is False
    assert ledger.effect is not None
    assert ledger.effect.status == "failed"
    assert "ValidationError" in ledger.effect.error
    assert "resolution must be one of" in ledger.effect.error


async def test_durable_replicate_clip_resolves_r2_reference_image_and_audio_uris(monkeypatch):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setenv("ORCH_PUBLIC_API_BASE_URL", "https://orchestrator.example")
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", "correlation-secret")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")

    submitted_clip_ref_image = None
    submitted_ls_audio = None
    submitted_ls_video = None

    async def async_create(*, model=None, version=None, input=None, **params):
        nonlocal submitted_clip_ref_image, submitted_ls_audio, submitted_ls_video
        if model == "lightricks/ltx-2.3-fast":
            submitted_clip_ref_image = input.get("image")
            return SimpleNamespace(
                id="pred-ltx-1",
                status="succeeded",
                output="https://cdn.replicate.com/ltx.mp4",
                error=None,
            )
        if model == "bytedance/latentsync" or version == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293":
            submitted_ls_audio = input.get("audio")
            submitted_ls_video = input.get("video")
            return SimpleNamespace(
                id="pred-ls-1",
                status="succeeded",
                output="https://cdn.replicate.com/latentsync.mp4",
                error=None,
            )
        raise ValueError(f"unknown model/version {model}/{version}")

    async def async_get(prediction_id):
        if prediction_id == "pred-ltx-1":
            return SimpleNamespace(id="pred-ltx-1", status="succeeded", output="https://cdn.replicate.com/ltx.mp4", error=None)
        if prediction_id == "pred-ls-1":
            return SimpleNamespace(id="pred-ls-1", status="succeeded", output="https://cdn.replicate.com/latentsync.mp4", error=None)
        raise ValueError(f"unknown prediction {prediction_id}")

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=lambda _id: None,
    )

    adapter = ReplicateVideoAdapter(
        tiers=[{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        clip={"resolution": "1080p", "aspect_ratio": "9:16", "fps": 25},
        latentsync={
            "enabled": True,
            "model": "bytedance/latentsync",
            "resolution": "720p",
            "max_retries": 3,
            "required": True,
            "cost_per_second": 0.003,
        },
        allow_mock_fallback=False,
    )

    class MultiEffectLedger:
        def __init__(self):
            self.effects: dict[str, SimpleNamespace] = {}
            self.reservations: list[dict] = []

        async def reserve(self, effect_key, **kwargs):
            self.reservations.append({"effect_key": effect_key, **kwargs})
            effect = SimpleNamespace(
                effect_key=effect_key,
                status="reserved",
                result=None,
                created=True,
                provider_operation_id=None,
                provider_status=None,
            )
            self.effects[effect_key] = effect
            return effect

        async def bind_provider_operation(self, effect_key, *, provider_operation_id, provider_status):
            effect = self.effects[effect_key]
            effect.provider_operation_id = provider_operation_id
            effect.provider_status = provider_status
            return effect

        async def update_provider_status(self, effect_key, *, provider_status, error_type=None):
            effect = self.effects[effect_key]
            effect.provider_status = provider_status
            return effect

        async def mark_succeeded(self, effect_key, *, result):
            effect = self.effects[effect_key]
            effect.status = "succeeded"
            effect.result = result
            return effect

        async def mark_failed(self, effect_key, *, error, release_quota, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "failed"
            return effect

        async def mark_uncertain(self, effect_key, *, error, error_type=None):
            effect = self.effects[effect_key]
            effect.status = "uncertain"
            return effect

        async def wait_for_provider_operation(self, effect_key, **_kwargs):
            return self.effects[effect_key]

        async def get(self, effect_key):
            return self.effects[effect_key]

    class FakeStorageResolver:
        async def get_signed_url(self, key: str) -> str:
            return f"https://signed-r2.example.com/{key}?token=valid"

    ledger = MultiEffectLedger()
    ctx = ToolContext(
        adapter=adapter,
        pipeline={"clip": {"timeout_ms": 100}, "video": {"reconciliation_poll_seconds": 0}},
        run={"organization_slug": "acme"},
        run_id="run-r2-resolve",
        effect_ledger=ledger,
        durable=True,
        storage_resolver=FakeStorageResolver(),
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=5,
        attempt=1,
        system_prompt="PRIVATE PROMPT",
        reference_image_uri="r2://generation-video/web-123/creator-0/image.png",
        audio_uri="r2://generation-video/web-123/item-1/voiceover.mp3",
        stage="talking_head",
    )

    assert artifact.uri == "https://cdn.replicate.com/latentsync.mp4"
    assert submitted_clip_ref_image == "https://signed-r2.example.com/web-123/creator-0/image.png?token=valid"
    assert submitted_ls_audio == "https://signed-r2.example.com/web-123/item-1/voiceover.mp3?token=valid"
    assert submitted_ls_video == "https://cdn.replicate.com/ltx.mp4"


async def test_non_durable_clip_resolves_r2_reference_image_and_audio_uris():
    called_kwargs = {}

    async def fake_generate_clip(*args, **kwargs):
        nonlocal called_kwargs
        called_kwargs = kwargs
        return Artifact(
            kind="clip",
            uri="https://cdn.replicate.com/clip.mp4",
            meta={"tier": kwargs.get("tier"), "seconds": kwargs.get("seconds")},
        )

    class FakeAdapter:
        generate_clip = fake_generate_clip

    class FakeStorage:
        async def get_signed_url(self, key: str) -> str:
            return f"https://signed-storage.example.com/{key}"

    ctx = ToolContext(
        adapter=FakeAdapter(),
        pipeline={},
        run={},
        run_id="run-nondurable-resolve",
        durable=False,
        storage=FakeStorage(),
    )

    artifact = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=5,
        attempt=1,
        reference_image_uri="r2://bucket/images/ref.png",
        audio_uri="r2://bucket/audio/voice.mp3",
    )

    assert artifact.uri == "https://cdn.replicate.com/clip.mp4"
    assert called_kwargs["reference_image_uri"] == "https://signed-storage.example.com/images/ref.png"
    assert called_kwargs["audio_uri"] == "https://signed-storage.example.com/audio/voice.mp3"


async def test_resolve_uri_for_provider_handles_all_schemes(tmp_path, monkeypatch):
    from orchestrator.tools.video import _resolve_uri_for_provider

    # 1. Empty / None
    ctx_empty = ToolContext(adapter=None, pipeline={}, run={}, run_id="test")
    assert await _resolve_uri_for_provider(None, ctx_empty) is None
    assert await _resolve_uri_for_provider("", ctx_empty) is None
    assert await _resolve_uri_for_provider("   ", ctx_empty) is None

    # 2. HTTP / HTTPS / Data
    assert await _resolve_uri_for_provider("https://example.com/pic.png", ctx_empty) == "https://example.com/pic.png"
    assert await _resolve_uri_for_provider("http://example.com/pic.png", ctx_empty) == "http://example.com/pic.png"
    assert await _resolve_uri_for_provider("data:image/png;base64,xyz", ctx_empty) == "data:image/png;base64,xyz"

    # 3. Local videos_root /videos/ and /media/
    vid_file = tmp_path / "clip.mp4"
    vid_file.write_bytes(b"dummy video content")
    media_file = tmp_path / "image.png"
    media_file.write_bytes(b"dummy image content")

    ctx_local = ToolContext(adapter=None, pipeline={}, run={}, run_id="test", videos_root=tmp_path)
    res_vid = await _resolve_uri_for_provider("/videos/clip.mp4", ctx_local)
    assert res_vid is not None and res_vid.startswith("data:video/mp4;base64,")

    res_media = await _resolve_uri_for_provider("/media/image.png", ctx_local)
    assert res_media is not None and res_media.startswith("data:image/png;base64,")

    # 4. Storage resolver variants
    # 4a. Callable
    ctx_callable = ToolContext(
        adapter=None, pipeline={}, run={}, run_id="test",
        storage_resolver=lambda u: f"https://callable.example.com/{u}"
    )
    assert await _resolve_uri_for_provider("r2://bucket/key.mp4", ctx_callable) == "https://callable.example.com/r2://bucket/key.mp4"

    # 4b. resolve_url
    class ResolverWithUrl:
        def resolve_url(self, u):
            return f"https://resolved-url.example.com/{u}"
    ctx_res_url = ToolContext(adapter=None, pipeline={}, run={}, run_id="test", storage_resolver=ResolverWithUrl())
    assert await _resolve_uri_for_provider("r2://bucket/key.mp4", ctx_res_url) == "https://resolved-url.example.com/r2://bucket/key.mp4"

    # 4c. resolve
    class ResolverWithResolve:
        def resolve(self, u):
            return f"https://resolved.example.com/{u}"
    ctx_res = ToolContext(adapter=None, pipeline={}, run={}, run_id="test", storage_resolver=ResolverWithResolve())
    assert await _resolve_uri_for_provider("r2://bucket/key.mp4", ctx_res) == "https://resolved.example.com/r2://bucket/key.mp4"

    # 5. Storage with get_signed_url_for
    class MultiStorageMock:
        async def get_signed_url_for(self, backend, key):
            return f"https://multi.example.com/{backend}/{key}"
    ctx_multi = ToolContext(adapter=None, pipeline={}, run={}, run_id="test", storage=MultiStorageMock())
    assert await _resolve_uri_for_provider("r2://my-bucket/path/file.mp4", ctx_multi) == "https://multi.example.com/r2/path/file.mp4"

    # 6. R2MediaStorage.from_env() fallback
    monkeypatch.setenv("R2_ACCOUNT_ID", "acc123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key123")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret123")
    monkeypatch.setenv("R2_BUCKET", "gen-bucket")

    class FakeR2Storage:
        async def get_signed_url(self, key):
            return f"https://r2-from-env.example.com/{key}?sig=ok"

    monkeypatch.setattr("orchestrator.storage.r2.R2MediaStorage.from_env", lambda: FakeR2Storage())
    ctx_r2_env = ToolContext(adapter=None, pipeline={}, run={}, run_id="test")
    assert await _resolve_uri_for_provider("r2://gen-bucket/web-1/image.png", ctx_r2_env) == "https://r2-from-env.example.com/web-1/image.png?sig=ok"
