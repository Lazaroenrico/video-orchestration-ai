"""Video generation tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Optional

import httpx
from replicate.exceptions import ReplicateError

from orchestrator.graph.state import Artifact
from orchestrator.replicate_webhook import build_effect_ref
from orchestrator.tools.base import ToolContext, require_artifact
from orchestrator.tracing import add_trace_metadata, traced

# Precedência explícita: a tool não conhece o conteúdo dos guardrails (montados por
# ``_video_prompt`` nos nodes), então declara que o brief acima manda em vez de tentar
# re-injetá-los. O agent refina a take; nunca revoga "No mock footage" (D33).
_REVISION_TEMPLATE = (
    "Revision directive (refine the take within the brief above; "
    "the brief and its constraints above always win):\n{revision}"
)
_TERMINAL_PREDICTION_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_AMBIGUOUS_CREATE_ERRORS = (
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
)


class VideoEffectError(RuntimeError):
    """Expected provider-effect failure that may be isolated to one item."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        uncertain: bool,
        error_type: str = "VideoEffectError",
        effect_key: str = "unknown",
        provider: str = "replicate",
    ) -> None:
        super().__init__(f"video provider operation failed ({code})")
        self.code = code
        self.retryable = retryable
        self.uncertain = uncertain
        self.error_type = error_type
        self.effect_key = effect_key
        self.provider = provider


def _sha256(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _webhook_url(ctx: ToolContext, effect_key: str) -> str:
    base_url = os.environ.get("ORCH_PUBLIC_API_BASE_URL", "").strip().rstrip("/")
    secret = os.environ.get("ORCH_WEBHOOK_CORRELATION_SECRET", "").strip()
    organization_slug = str(
        ctx.run.get("organization_slug")
        or getattr(ctx.effect_ledger, "organization_slug", "")
        or os.environ.get("ORCH_ORGANIZATION_SLUG", "")
    ).strip()
    if not base_url or not secret or not organization_slug:
        raise RuntimeError(
            "durable Replicate video requires public URL, organization and correlation secret"
        )
    return (
        f"{base_url}/webhooks/replicate/{organization_slug}/"
        f"{build_effect_ref(organization_slug, effect_key, secret)}"
    )


def _video_timeout_seconds(ctx: ToolContext) -> float:
    clip = ctx.pipeline.get("clip", {}) if isinstance(ctx.pipeline, dict) else {}
    return max(float(clip.get("timeout_ms", 900_000)) / 1000, 0.001)


def _video_poll_seconds(ctx: ToolContext) -> float:
    video = ctx.pipeline.get("video", {}) if isinstance(ctx.pipeline, dict) else {}
    return max(float(video.get("reconciliation_poll_seconds", 1.0)), 0.0)


async def _durable_replicate_clip(
    ctx: ToolContext,
    *,
    item_id: str,
    tier: str,
    seconds: int,
    attempt: int,
    system_prompt: Optional[str],
    reference_image_uri: Optional[str],
    stage: str,
) -> Artifact:
    if os.environ.get("ORCH_ENABLE_PAID_ADAPTERS", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise RuntimeError("durable paid adapters require ORCH_ENABLE_PAID_ADAPTERS=true")
    ledger = ctx.effect_ledger
    if ledger is None:
        raise RuntimeError("durable paid adapters require PostgresEffectLedger")

    prompt_hash = _sha256(system_prompt)
    reference_hash = _sha256(reference_image_uri)
    request = {
        "model": ctx.adapter.clip_model(tier),
        "tier": tier,
        "seconds": seconds,
        "attempt": attempt,
        "prompt_hash": prompt_hash,
        "reference_hash": reference_hash,
    }
    request_hash = _sha256(json.dumps(request, sort_keys=True, separators=(",", ":")))
    effect_key = f"video:{ctx.run_id}:{item_id}:{stage}:{attempt}:{request_hash}"
    deadline = time.monotonic() + _video_timeout_seconds(ctx)

    try:
        reservation = await ledger.reserve(
            effect_key,
            run_id=ctx.run_id,
            provider="replicate_video_seconds",
            units=seconds,
            request=request,
        )
    except Exception as exc:
        if type(exc).__name__ != "UncertainEffectError" or not hasattr(ledger, "get"):
            raise
        reservation = await ledger.get(effect_key)

    if reservation.status == "succeeded":
        result = reservation.result or {}
        artifact_data = result.get("artifact") if isinstance(result, dict) else None
        if not artifact_data:
            raise RuntimeError(f"paid video effect {effect_key!r} has no replay artifact")
        return Artifact.model_validate(artifact_data)

    prediction_id = getattr(reservation, "provider_operation_id", None)
    ambiguity_error_type = getattr(reservation, "error_type", None)
    if reservation.created:
        try:
            prediction = await ctx.adapter.submit_clip_prediction(
                item_id=item_id,
                tier=tier,
                seconds=seconds,
                attempt=attempt,
                system_prompt=system_prompt,
                reference_image_uri=reference_image_uri,
                webhook_url=_webhook_url(ctx, effect_key),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            await ledger.mark_failed(
                effect_key,
                error=type(exc).__name__,
                error_type=type(exc).__name__,
                release_quota=True,
            )
            raise VideoEffectError(
                "provider_unreachable",
                retryable=True,
                uncertain=False,
                error_type=type(exc).__name__,
                effect_key=effect_key,
            ) from exc
        except (httpx.HTTPStatusError, ReplicateError) as exc:
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else getattr(exc, "status", None)
            )
            error_type = type(exc).__name__
            if isinstance(status, int) and 400 <= status < 500:
                await ledger.mark_failed(
                    effect_key,
                    error=error_type,
                    error_type=error_type,
                    release_quota=True,
                )
                raise VideoEffectError(
                    "provider_rejected",
                    retryable=status == 429,
                    uncertain=False,
                    error_type=error_type,
                    effect_key=effect_key,
                ) from exc
            ambiguity_error_type = error_type
            await ledger.mark_uncertain(
                effect_key,
                error=error_type,
                error_type=error_type,
            )
            reservation = await ledger.wait_for_provider_operation(
                effect_key,
                timeout_seconds=max(deadline - time.monotonic(), 0),
                poll_interval_seconds=_video_poll_seconds(ctx),
            )
            prediction_id = reservation.provider_operation_id
        except _AMBIGUOUS_CREATE_ERRORS as exc:
            error_type = type(exc).__name__
            ambiguity_error_type = error_type
            await ledger.mark_uncertain(
                effect_key,
                error=error_type,
                error_type=error_type,
            )
            reservation = await ledger.wait_for_provider_operation(
                effect_key,
                timeout_seconds=max(deadline - time.monotonic(), 0),
                poll_interval_seconds=_video_poll_seconds(ctx),
            )
            prediction_id = reservation.provider_operation_id
        else:
            prediction_id = prediction.id
            await ledger.bind_provider_operation(
                effect_key,
                provider_operation_id=prediction.id,
                provider_status=prediction.status,
            )
    elif not prediction_id:
        reservation = await ledger.wait_for_provider_operation(
            effect_key,
            timeout_seconds=max(deadline - time.monotonic(), 0),
            poll_interval_seconds=_video_poll_seconds(ctx),
        )
        prediction_id = reservation.provider_operation_id

    if not prediction_id:
        raise VideoEffectError(
            "prediction_id_unresolved",
            retryable=False,
            uncertain=True,
            error_type=ambiguity_error_type or "PredictionIdUnresolved",
            effect_key=effect_key,
        )

    prediction = None
    while time.monotonic() < deadline:
        try:
            prediction = await ctx.adapter.get_video_prediction(prediction_id)
        except (httpx.TransportError, httpx.HTTPStatusError, ReplicateError) as exc:
            error_type = type(exc).__name__
            await ledger.mark_uncertain(
                effect_key,
                error=error_type,
                error_type=error_type,
            )
            raise VideoEffectError(
                "prediction_poll_unavailable",
                retryable=True,
                uncertain=True,
                error_type=error_type,
                effect_key=effect_key,
            ) from exc
        await ledger.update_provider_status(
            effect_key,
            provider_status=prediction.status,
        )
        if prediction.status in _TERMINAL_PREDICTION_STATUSES:
            break
        await asyncio.sleep(_video_poll_seconds(ctx))

    if prediction is None or prediction.status not in _TERMINAL_PREDICTION_STATUSES:
        try:
            canceled = await ctx.adapter.cancel_video_prediction(prediction_id)
            await ledger.update_provider_status(
                effect_key,
                provider_status=canceled.status,
                error_type="PredictionTimeout",
            )
            if canceled.status in {"failed", "canceled"}:
                await ledger.mark_failed(
                    effect_key,
                    error=f"provider_{canceled.status}",
                    error_type="PredictionTimeout",
                    release_quota=False,
                )
                raise VideoEffectError(
                    f"prediction_{canceled.status}",
                    retryable=False,
                    uncertain=False,
                    error_type="PredictionTimeout",
                    effect_key=effect_key,
                )
            if canceled.status == "succeeded":
                prediction = canceled
        except VideoEffectError:
            raise
        except (httpx.TransportError, httpx.HTTPStatusError, ReplicateError):
            canceled = None
        if prediction is not None and prediction.status == "succeeded":
            try:
                artifact = ctx.adapter.clip_artifact_from_prediction(
                    prediction,
                    tier=tier,
                    seconds=seconds,
                    attempt=attempt,
                    reference_image_uri=reference_image_uri,
                )
            except RuntimeError as exc:
                await ledger.mark_failed(
                    effect_key,
                    error=type(exc).__name__,
                    error_type=type(exc).__name__,
                    release_quota=False,
                )
                raise VideoEffectError(
                    "invalid_prediction_output",
                    retryable=False,
                    uncertain=False,
                    error_type=type(exc).__name__,
                    effect_key=effect_key,
                ) from exc
            await ledger.mark_succeeded(
                effect_key,
                result={
                    "provider_prediction_id": prediction.id,
                    "artifact": artifact.model_dump(mode="json"),
                },
            )
            return artifact
        await ledger.mark_uncertain(
            effect_key,
            error="PredictionTimeout",
            error_type="PredictionTimeout",
        )
        raise VideoEffectError(
            "prediction_timeout",
            retryable=False,
            uncertain=True,
            error_type="PredictionTimeout",
            effect_key=effect_key,
        )

    if prediction.status != "succeeded":
        await ledger.mark_failed(
            effect_key,
            error=f"provider_{prediction.status}",
            error_type="ReplicatePredictionError",
            release_quota=False,
        )
        raise VideoEffectError(
            f"prediction_{prediction.status}",
            retryable=False,
            uncertain=False,
            error_type="ReplicatePredictionError",
            effect_key=effect_key,
        )

    try:
        artifact = ctx.adapter.clip_artifact_from_prediction(
            prediction,
            tier=tier,
            seconds=seconds,
            attempt=attempt,
            reference_image_uri=reference_image_uri,
        )
    except RuntimeError as exc:
        await ledger.mark_failed(
            effect_key,
            error=type(exc).__name__,
            error_type=type(exc).__name__,
            release_quota=False,
        )
        raise VideoEffectError(
            "invalid_prediction_output",
            retryable=False,
            uncertain=False,
            error_type=type(exc).__name__,
            effect_key=effect_key,
        ) from exc
    await ledger.mark_succeeded(
        effect_key,
        result={
            "provider_prediction_id": prediction.id,
            "artifact": artifact.model_dump(mode="json"),
        },
    )
    return artifact


def _compose_prompt(system_prompt: Optional[str], revision: Optional[str]) -> Optional[str]:
    """Apenda a diretiva do agent ao brief server-authored, preservando-o intacto."""
    directive = (revision or "").strip()
    if not directive:
        return system_prompt
    block = _REVISION_TEMPLATE.format(revision=directive)
    return f"{system_prompt}\n\n{block}" if system_prompt else block


@traced(
    "tool.generate_clip",
    run_type="tool",
    tool_name="generate_clip",
    role="video",
    stage="video",
)
async def generate_clip_tool(
    ctx: ToolContext,
    *,
    item_id: str,
    tier: str,
    seconds: int,
    attempt: int,
    system_prompt: Optional[str] = None,
    reference_image_uri: Optional[str] = None,
    revision: Optional[str] = None,
    stage: str = "video",
) -> Artifact:
    """Gera um clip. ``revision`` é a única alavanca do agent (D33): uma diretiva
    apendada ao brief; todo o resto é server-authoritative.
    """
    add_trace_metadata(
        tool_name="generate_clip",
        role="video",
        stage=stage,
        run_id=ctx.run_id,
        item_id=item_id,
        tier=tier,
        has_revision=bool((revision or "").strip()),
    )
    prompt = _compose_prompt(system_prompt, revision)
    if ctx.durable and hasattr(ctx.adapter, "submit_clip_prediction"):
        clip = await _durable_replicate_clip(
            ctx,
            item_id=item_id,
            tier=tier,
            seconds=seconds,
            attempt=attempt,
            system_prompt=prompt,
            reference_image_uri=reference_image_uri,
            stage=stage,
        )
    else:
        clip = await ctx.adapter.generate_clip(
            item_id=item_id,
            tier=tier,
            seconds=seconds,
            attempt=attempt,
            system_prompt=prompt,
            reference_image_uri=reference_image_uri,
        )
    return require_artifact(clip, tool_name="generate_clip_tool")
