"""Video generation tools."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

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


async def _durable_prediction_lifecycle(
    ctx: ToolContext,
    *,
    effect_key: str,
    provider: str,
    units: int,
    request: dict[str, Any],
    submit_fn: Callable[[str], Awaitable[Any]],
    artifact_fn: Callable[[Any], Artifact],
    persist_fn: Callable[[Artifact], Awaitable[Artifact]] | None = None,
) -> Artifact:
    ledger = ctx.effect_ledger
    if ledger is None:
        raise RuntimeError("durable paid adapters require PostgresEffectLedger")

    deadline = time.monotonic() + _video_timeout_seconds(ctx)

    try:
        reservation = await ledger.reserve(
            effect_key,
            run_id=ctx.run_id,
            provider=provider,
            units=units,
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
            prediction = await submit_fn(_webhook_url(ctx, effect_key))
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
                artifact = artifact_fn(prediction)
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
            if persist_fn is not None:
                artifact = await persist_fn(artifact)
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
        artifact = artifact_fn(prediction)
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
    if persist_fn is not None:
        artifact = await persist_fn(artifact)
    await ledger.mark_succeeded(
        effect_key,
        result={
            "provider_prediction_id": prediction.id,
            "artifact": artifact.model_dump(mode="json"),
        },
    )
    return artifact


def _build_latentsync_artifact(
    adapter: Any,
    prediction: Any,
    *,
    base_artifact: Artifact,
) -> Artifact:
    if hasattr(adapter, "latentsync_artifact_from_prediction"):
        return adapter.latentsync_artifact_from_prediction(
            prediction,
            base_artifact=base_artifact,
        )
    if prediction.status != "succeeded":
        detail = getattr(prediction, "error", None) or prediction.status
        raise RuntimeError(f"Replicate LatentSync prediction did not succeed: {detail}")
    from orchestrator.adapters.replicate_video import ReplicateVideoAdapter

    uri = ReplicateVideoAdapter._coerce_output(prediction.output)
    meta = dict(base_artifact.meta)
    meta["latentsync_applied"] = True
    meta["latentsync_model"] = getattr(adapter, "latentsync_model", "bytedance/latentsync")
    meta["prediction_id"] = prediction.id
    meta["base_clip_uri"] = base_artifact.uri
    seconds = int(base_artifact.meta.get("seconds") or 0)
    base_cost = float(base_artifact.meta.get("cost_usd") or 0.0)
    ls_cost_per_sec = float(getattr(adapter, "latentsync_cost_per_second", 0.003))
    ls_cost = round(ls_cost_per_sec * seconds, 4)
    meta["latentsync_cost_usd"] = ls_cost
    meta["cost_usd"] = round(base_cost + ls_cost, 4)
    return Artifact(
        kind="clip",
        uri=uri,
        meta=meta,
    )


async def _resolve_base_video_url_for_provider(
    base_artifact: Artifact,
    ctx: ToolContext,
) -> str:
    uri = base_artifact.uri if isinstance(base_artifact.uri, str) else ""

    if ctx.storage_resolver is not None:
        if callable(ctx.storage_resolver):
            res = ctx.storage_resolver(uri)
            if asyncio.iscoroutine(res):
                res = await res
            if res:
                return str(res)
        elif hasattr(ctx.storage_resolver, "resolve_url"):
            res = ctx.storage_resolver.resolve_url(uri)
            if asyncio.iscoroutine(res):
                res = await res
            if res:
                return str(res)
        elif hasattr(ctx.storage_resolver, "resolve"):
            res = ctx.storage_resolver.resolve(uri)
            if asyncio.iscoroutine(res):
                res = await res
            if res:
                return str(res)
        elif hasattr(ctx.storage_resolver, "get_signed_url"):
            from orchestrator.storage.resolve import object_pointer_from_uri

            pointer = object_pointer_from_uri(uri)
            if pointer:
                _, key = pointer
                return await ctx.storage_resolver.get_signed_url(key)

    if (
        ctx.videos_root is not None
        and uri.startswith("/videos/")
    ):
        vpath = Path(ctx.videos_root) / uri[len("/videos/"):]
        if vpath.is_file():
            mime = mimetypes.guess_type(vpath.name)[0] or "video/mp4"
            payload = base64.b64encode(vpath.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{payload}"

    if uri.startswith("data:") or (
        (uri.startswith("http://") or uri.startswith("https://"))
        and not uri.startswith("r2://")
        and not uri.startswith("s3://")
    ):
        return uri

    source_uri = base_artifact.meta.get("source_uri") if base_artifact.meta else None
    if isinstance(source_uri, str) and (
        source_uri.startswith("http://")
        or source_uri.startswith("https://")
        or source_uri.startswith("data:")
    ):
        return source_uri

    return uri


async def _durable_replicate_clip(
    ctx: ToolContext,
    *,
    item_id: str,
    tier: str,
    seconds: int,
    attempt: int,
    system_prompt: Optional[str],
    reference_image_uri: Optional[str],
    audio_uri: Optional[str] = None,
    stage: str,
) -> Artifact:
    if os.environ.get("ORCH_ENABLE_PAID_ADAPTERS", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise RuntimeError("durable paid adapters require ORCH_ENABLE_PAID_ADAPTERS=true")
    if ctx.effect_ledger is None:
        raise RuntimeError("durable paid adapters require PostgresEffectLedger")

    prompt_hash = _sha256(system_prompt)
    reference_hash = _sha256(reference_image_uri)
    base_request = {
        "model": ctx.adapter.clip_model(tier),
        "tier": tier,
        "seconds": seconds,
        "attempt": attempt,
        "prompt_hash": prompt_hash,
        "reference_hash": reference_hash,
    }
    base_request_hash = _sha256(json.dumps(base_request, sort_keys=True, separators=(",", ":")))
    base_effect_key = f"video:{ctx.run_id}:{item_id}:{stage}:{attempt}:{base_request_hash}"

    if getattr(ctx.adapter, "latentsync_required", False) and stage != "product_demo":
        if not audio_uri:
            raise VideoEffectError(
                "latentsync_audio_missing",
                retryable=False,
                uncertain=False,
                error_type="LatentSyncRequiredError",
                effect_key=base_effect_key,
            )
        if not getattr(ctx.adapter, "latentsync_enabled", False):
            raise VideoEffectError(
                "latentsync_disabled",
                retryable=False,
                uncertain=False,
                error_type="LatentSyncRequiredError",
                effect_key=base_effect_key,
            )

    has_latentsync = bool(audio_uri and getattr(ctx.adapter, "latentsync_enabled", False))
    stage_1_basename = f"base-clip-{attempt}" if has_latentsync else f"clip-{attempt}"
    stage_1_kind = "base_clip" if has_latentsync else "clip"

    async def _persist_base(art: Artifact) -> Artifact:
        if ctx.storage is None and ctx.videos_root is None:
            return art
        from orchestrator.media_store import persist_artifact_from_url

        return await persist_artifact_from_url(
            art,
            run_id=ctx.run_id,
            item_id=item_id,
            basename=stage_1_basename,
            kind=stage_1_kind,
            videos_root=ctx.videos_root,
            storage=ctx.storage,
            db=ctx.artifact_db,
        )

    base_artifact = await _durable_prediction_lifecycle(
        ctx,
        effect_key=base_effect_key,
        provider="replicate_video_seconds",
        units=seconds,
        request=base_request,
        submit_fn=lambda webhook_url: ctx.adapter.submit_clip_prediction(
            item_id=item_id,
            tier=tier,
            seconds=seconds,
            attempt=attempt,
            system_prompt=system_prompt,
            reference_image_uri=reference_image_uri,
            webhook_url=webhook_url,
        ),
        artifact_fn=lambda pred: ctx.adapter.clip_artifact_from_prediction(
            pred,
            tier=tier,
            seconds=seconds,
            attempt=attempt,
            reference_image_uri=reference_image_uri,
        ),
        persist_fn=_persist_base,
    )

    if has_latentsync:
        input_video_uri = await _resolve_base_video_url_for_provider(base_artifact, ctx)
        ls_model = str(getattr(ctx.adapter, "latentsync_model", "bytedance/latentsync"))
        ls_resolution = str(getattr(ctx.adapter, "latentsync_resolution", "720p"))
        ls_request = {
            "model": ls_model,
            "video_hash": _sha256(base_artifact.uri),
            "audio_hash": _sha256(audio_uri),
            "resolution": ls_resolution,
            "seconds": seconds,
            "attempt": attempt,
        }
        ls_request_hash = _sha256(json.dumps(ls_request, sort_keys=True, separators=(",", ":")))
        ls_effect_key = f"latentsync:{ctx.run_id}:{item_id}:{stage}:{attempt}:{ls_request_hash}"

        async def _persist_final(art: Artifact) -> Artifact:
            if "base_clip_uri" not in art.meta:
                meta = dict(art.meta)
                meta["base_clip_uri"] = base_artifact.uri
                art = art.model_copy(update={"meta": meta})
            if ctx.storage is None and ctx.videos_root is None:
                return art
            from orchestrator.media_store import persist_artifact_from_url

            return await persist_artifact_from_url(
                art,
                run_id=ctx.run_id,
                item_id=item_id,
                basename=f"clip-{attempt}",
                kind="clip",
                videos_root=ctx.videos_root,
                storage=ctx.storage,
                db=ctx.artifact_db,
            )

        final_artifact = await _durable_prediction_lifecycle(
            ctx,
            effect_key=ls_effect_key,
            provider="replicate_video_seconds",
            units=seconds,
            request=ls_request,
            submit_fn=lambda webhook_url: ctx.adapter.submit_latentsync_prediction(
                video_uri=input_video_uri,
                audio_uri=audio_uri,
                resolution=ls_resolution,
                webhook_url=webhook_url,
            ),
            artifact_fn=lambda pred: _build_latentsync_artifact(
                ctx.adapter,
                pred,
                base_artifact=base_artifact,
            ),
            persist_fn=_persist_final,
        )
        if "base_clip_uri" not in final_artifact.meta:
            final_meta = dict(final_artifact.meta)
            final_meta["base_clip_uri"] = base_artifact.uri
            final_artifact = final_artifact.model_copy(update={"meta": final_meta})
        return final_artifact

    return base_artifact


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
    audio_uri: Optional[str] = None,
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
        has_audio=bool(audio_uri),
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
            audio_uri=audio_uri,
            stage=stage,
        )
    else:
        gen_fn = ctx.adapter.generate_clip
        call_kwargs: dict[str, Any] = {
            "item_id": item_id,
            "tier": tier,
            "seconds": seconds,
            "attempt": attempt,
            "system_prompt": prompt,
            "reference_image_uri": reference_image_uri,
            "audio_uri": audio_uri,
        }
        try:
            sig = inspect.signature(gen_fn)
            if "stage" in sig.parameters:
                call_kwargs["stage"] = stage
        except (ValueError, TypeError):
            pass

        try:
            clip = await gen_fn(**call_kwargs)
        except TypeError as exc:
            if "stage" in call_kwargs and ("stage" in str(exc) or "unexpected keyword" in str(exc)):
                call_kwargs.pop("stage", None)
                clip = await gen_fn(**call_kwargs)
            else:
                raise
    return require_artifact(clip, tool_name="generate_clip_tool")
