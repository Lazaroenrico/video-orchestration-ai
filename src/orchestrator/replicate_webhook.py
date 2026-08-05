"""Signed correlation and monotonic handling for Replicate prediction webhooks."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping


class ReplicateWebhookError(ValueError):
    """Webhook input could not be authenticated or safely correlated."""


@dataclass(frozen=True)
class ReplicateWebhookEvent:
    prediction_id: str
    status: str


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, binascii.Error) as exc:
        raise ReplicateWebhookError("invalid webhook correlation") from exc


def build_effect_ref(organization_slug: str, effect_key: str, secret: str) -> str:
    encoded = _b64url_encode(effect_key.encode())
    digest = hmac.new(
        secret.encode(),
        f"{organization_slug}:{effect_key}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_b64url_encode(digest)}"


def decode_effect_ref(organization_slug: str, effect_ref: str, secret: str) -> str:
    try:
        encoded, supplied_signature = effect_ref.split(".", 1)
    except ValueError as exc:
        raise ReplicateWebhookError("invalid webhook correlation") from exc
    try:
        effect_key = _b64url_decode(encoded).decode()
    except UnicodeDecodeError as exc:
        raise ReplicateWebhookError("invalid webhook correlation") from exc
    expected = build_effect_ref(organization_slug, effect_key, secret).split(".", 1)[1]
    if not hmac.compare_digest(supplied_signature, expected):
        raise ReplicateWebhookError("invalid webhook correlation")
    return effect_key


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = name.casefold()
    for key, value in headers.items():
        if key.casefold() == lowered:
            return str(value)
    raise ReplicateWebhookError(f"missing {name} header")


def _signing_key(signing_secret: str) -> bytes:
    encoded = signing_secret.removeprefix("whsec_")
    if not encoded or encoded == signing_secret:
        raise ReplicateWebhookError("invalid webhook signing secret")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReplicateWebhookError("invalid webhook signing secret") from exc


def parse_and_verify_replicate_event(
    raw_body: bytes,
    headers: Mapping[str, str],
    *,
    signing_secret: str,
    now: float | None = None,
    tolerance_seconds: int = 300,
) -> ReplicateWebhookEvent:
    webhook_id = _header(headers, "webhook-id")
    timestamp_text = _header(headers, "webhook-timestamp")
    signatures = _header(headers, "webhook-signature")
    try:
        timestamp = int(timestamp_text)
    except ValueError as exc:
        raise ReplicateWebhookError("invalid webhook timestamp") from exc
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise ReplicateWebhookError("webhook timestamp is outside tolerance")

    signed = f"{webhook_id}.{timestamp_text}.".encode() + raw_body
    expected = base64.b64encode(
        hmac.new(_signing_key(signing_secret), signed, hashlib.sha256).digest()
    ).decode()
    candidates = [
        token.split(",", 1)[1]
        for token in signatures.split()
        if token.startswith("v1,") and "," in token
    ]
    if not candidates or not any(hmac.compare_digest(value, expected) for value in candidates):
        raise ReplicateWebhookError("invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplicateWebhookError("invalid webhook JSON") from exc
    if not isinstance(payload, dict):
        raise ReplicateWebhookError("invalid webhook JSON object")
    prediction_id = str(payload.get("id") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    if status == "cancelled":
        status = "canceled"
    if not prediction_id:
        raise ReplicateWebhookError("webhook prediction id is missing")
    if status not in {"starting", "processing", "succeeded", "failed", "canceled"}:
        raise ReplicateWebhookError("webhook prediction status is invalid")
    return ReplicateWebhookEvent(prediction_id=prediction_id, status=status)


async def apply_replicate_event(
    ledger: Any,
    effect_key: str,
    event: ReplicateWebhookEvent,
) -> bool:
    """Apply one event without regressing state; return whether state changed."""
    before = await ledger.get(effect_key)
    before_values = (
        before.status,
        before.provider_operation_id,
        before.provider_status,
    )
    if before.provider_operation_id is None:
        after = await ledger.bind_provider_operation(
            effect_key,
            provider_operation_id=event.prediction_id,
            provider_status=event.status,
        )
    else:
        if before.provider_operation_id != event.prediction_id:
            raise ReplicateWebhookError("prediction id does not match correlated effect")
        after = await ledger.update_provider_status(
            effect_key,
            provider_status=event.status,
            error_type=(
                "ReplicatePredictionError"
                if event.status in {"failed", "canceled"}
                else None
            ),
        )
    changed = before_values != (
        after.status,
        after.provider_operation_id,
        after.provider_status,
    )
    if (
        after.provider_status in {"failed", "canceled"}
        and after.status != "failed"
    ):
        await ledger.mark_failed(
            effect_key,
            error=f"provider_{after.provider_status}",
            error_type="ReplicatePredictionError",
            release_quota=False,
        )
        changed = True
    return changed
