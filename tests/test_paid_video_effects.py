from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.tools.base import ToolContext
from orchestrator.tools.video import generate_clip_tool


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
