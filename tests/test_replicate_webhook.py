from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from orchestrator.replicate_webhook import (
    ReplicateWebhookError,
    apply_replicate_event,
    build_effect_ref,
    decode_effect_ref,
    parse_and_verify_replicate_event,
)

SIGNING_KEY = b"replicate-signing-key"
SIGNING_SECRET = "whsec_" + base64.b64encode(SIGNING_KEY).decode()
CORRELATION_SECRET = "correlation-secret"


def _signed_event(payload: dict, *, timestamp: int = 1_700_000_000):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    webhook_id = "msg-1"
    signed = f"{webhook_id}.{timestamp}.".encode() + raw
    signature = base64.b64encode(
        hmac.new(SIGNING_KEY, signed, hashlib.sha256).digest()
    ).decode()
    headers = {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
    }
    return raw, headers


def test_effect_ref_is_tenant_bound_and_tamper_evident():
    key = "video:run-1:item-1:talking_head:0:hash"
    ref = build_effect_ref("acme", key, CORRELATION_SECRET)

    assert decode_effect_ref("acme", ref, CORRELATION_SECRET) == key
    with pytest.raises(ReplicateWebhookError, match="correlation"):
        decode_effect_ref("globex", ref, CORRELATION_SECRET)
    with pytest.raises(ReplicateWebhookError, match="correlation"):
        decode_effect_ref("acme", ref[:-1] + ("A" if ref[-1] != "A" else "B"), CORRELATION_SECRET)


def test_replicate_signature_accepts_valid_raw_body_and_rejects_invalid_or_stale():
    payload = {"id": "prediction-1", "status": "succeeded", "output": "https://cdn/x.mp4"}
    raw, headers = _signed_event(payload)

    event = parse_and_verify_replicate_event(
        raw,
        headers,
        signing_secret=SIGNING_SECRET,
        now=1_700_000_100,
    )
    assert event.prediction_id == "prediction-1"
    assert event.status == "succeeded"

    invalid = {**headers, "webhook-signature": "v1,invalid"}
    with pytest.raises(ReplicateWebhookError, match="signature"):
        parse_and_verify_replicate_event(
            raw,
            invalid,
            signing_secret=SIGNING_SECRET,
            now=1_700_000_100,
        )
    with pytest.raises(ReplicateWebhookError, match="timestamp"):
        parse_and_verify_replicate_event(
            raw,
            headers,
            signing_secret=SIGNING_SECRET,
            now=1_700_000_301,
        )


class FakeLedger:
    def __init__(self):
        self.effect = SimpleNamespace(
            effect_key="effect",
            run_id="run-1",
            provider="replicate_video_seconds",
            status="uncertain",
            provider_operation_id=None,
            provider_status=None,
        )
        self.events: list[tuple[str, str]] = []
        self.failed = 0

    async def get(self, _effect_key):
        return self.effect

    async def bind_provider_operation(self, _effect_key, *, provider_operation_id, provider_status):
        self.effect.provider_operation_id = provider_operation_id
        self.effect.provider_status = provider_status
        self.effect.status = "reserved"
        self.events.append(("bind", provider_status))
        return self.effect

    async def update_provider_status(self, _effect_key, *, provider_status, error_type=None):
        terminal = {"succeeded", "failed", "canceled"}
        current = self.effect.provider_status
        if current not in terminal:
            if provider_status in terminal or current != "processing":
                self.effect.provider_status = provider_status
        self.events.append(("status", self.effect.provider_status))
        return self.effect

    async def mark_failed(self, _effect_key, **_kwargs):
        if self.effect.status != "failed":
            self.failed += 1
        self.effect.status = "failed"
        return self.effect


async def test_webhook_application_is_idempotent_and_ignores_terminal_regression():
    ledger = FakeLedger()
    started = SimpleNamespace(prediction_id="prediction-1", status="processing")
    completed = SimpleNamespace(prediction_id="prediction-1", status="succeeded")
    stale = SimpleNamespace(prediction_id="prediction-1", status="starting")

    assert await apply_replicate_event(ledger, "effect", started) is True
    assert await apply_replicate_event(ledger, "effect", started) is False
    assert await apply_replicate_event(ledger, "effect", completed) is True
    assert await apply_replicate_event(ledger, "effect", stale) is False
    assert ledger.effect.provider_status == "succeeded"


async def test_failed_webhook_is_terminal_and_duplicate_does_not_repeat_transition():
    ledger = FakeLedger()
    event = SimpleNamespace(prediction_id="prediction-2", status="failed")

    assert await apply_replicate_event(ledger, "effect", event) is True
    assert await apply_replicate_event(ledger, "effect", event) is False
    assert ledger.effect.status == "failed"
    assert ledger.failed == 1


async def test_public_webhook_route_authenticates_correlates_and_emits_safe_event(
    monkeypatch,
):
    from orchestrator.web import server

    ledger = FakeLedger()

    class Events:
        def __init__(self):
            self.entries = []

        async def append_event(self, run_id, event_type, data):
            self.entries.append((run_id, event_type, data))

    events = Events()
    monkeypatch.setenv("REPLICATE_WEBHOOK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", CORRELATION_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

    async def repositories(_organization_slug):
        return ledger, events

    monkeypatch.setattr(server, "_replicate_webhook_repositories", repositories)
    effect_ref = build_effect_ref("acme", "effect", CORRELATION_SECRET)
    raw, headers = _signed_event(
        {"id": "prediction-1", "status": "processing", "input": {"prompt": "PRIVATE"}},
        timestamp=int(time.time()),
    )

    class Request:
        async def body(self):
            return raw

    request = Request()
    request.headers = headers
    first = await server.replicate_prediction_webhook("acme", effect_ref, request)
    duplicate = await server.replicate_prediction_webhook("acme", effect_ref, request)

    assert first == {"ok": True, "changed": True}
    assert duplicate == {"ok": True, "changed": False}
    assert len(events.entries) == 1
    serialized = repr(events.entries[0])
    assert "PRIVATE" not in serialized
    assert "input" not in serialized


async def test_public_webhook_route_rejects_invalid_signature(monkeypatch):
    from fastapi import HTTPException

    from orchestrator.web import server

    monkeypatch.setenv("REPLICATE_WEBHOOK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("ORCH_WEBHOOK_CORRELATION_SECRET", CORRELATION_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    effect_ref = build_effect_ref("acme", "effect", CORRELATION_SECRET)
    raw, headers = _signed_event(
        {"id": "prediction-1", "status": "processing"},
        timestamp=int(time.time()),
    )
    headers["webhook-signature"] = "v1,invalid"

    class Request:
        async def body(self):
            return raw

    request = Request()
    request.headers = headers
    with pytest.raises(HTTPException) as error:
        await server.replicate_prediction_webhook("acme", effect_ref, request)
    assert error.value.status_code == 401
