"""Shared primitives for the thin node -> tool -> adapter layer."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from langchain_core.runnables import RunnableConfig

from orchestrator.graph.state import Artifact, QCResult


class ToolOutputError(RuntimeError):
    """Raised when an adapter returns a shape a tool cannot safely pass downstream."""


@dataclass(frozen=True)
class ToolContext:
    adapter: Any
    pipeline: dict[str, Any]
    run: dict[str, Any]
    run_id: str
    language_runtime: Any | None = None
    effect_ledger: Any | None = None
    durable: bool = False


def tool_context_from_config(config: RunnableConfig) -> ToolContext:
    """Extract the already-resolved adapter and runtime knobs from RunnableConfig."""
    configurable = config["configurable"]
    return ToolContext(
        adapter=configurable["adapter"],
        language_runtime=configurable.get("language_runtime"),
        pipeline=configurable.get("pipeline", {}),
        run=configurable.get("run", {}),
        run_id=configurable.get("thread_id", "run"),
        effect_ledger=configurable.get("effect_ledger"),
        durable=bool(configurable.get("durable", False)),
    )


def direct_elevenlabs_voice_enabled(ctx: ToolContext) -> bool:
    voice = ctx.pipeline.get("voice") if isinstance(ctx.pipeline, dict) else None
    return bool(
        isinstance(voice, dict)
        and voice.get("mode") == "designed"
        and voice.get("provider") == "elevenlabs"
    )


def _definitely_not_billed(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
    ):
        return True
    return bool(
        isinstance(exc, httpx.HTTPStatusError)
        and 400 <= exc.response.status_code < 500
    )


async def execute_paid_effect(
    ctx: ToolContext,
    *,
    effect_key: str,
    provider: str,
    units: int,
    request: dict[str, Any],
    operation: Callable[[], Awaitable[dict[str, Any]]],
    reconcile: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run one durable paid effect through quota reservation and replay."""
    if not ctx.durable:
        return await operation()
    if os.environ.get("ORCH_ENABLE_PAID_ADAPTERS", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise RuntimeError("durable paid adapters require ORCH_ENABLE_PAID_ADAPTERS=true")
    ledger = ctx.effect_ledger
    if ledger is None:
        raise RuntimeError("durable paid adapters require PostgresEffectLedger")

    try:
        reservation = await ledger.reserve(
            effect_key,
            run_id=ctx.run_id,
            provider=provider,
            units=units,
            request=request,
        )
    except Exception as exc:
        if type(exc).__name__ != "UncertainEffectError" or reconcile is None:
            raise
        reconciled = await reconcile()
        await ledger.mark_reconciled(effect_key, result=reconciled)
        return reconciled
    if reservation.status == "succeeded":
        if not isinstance(reservation.result, dict) or not reservation.result:
            raise RuntimeError(f"paid effect {effect_key!r} has no replay result")
        return reservation.result
    if reservation.status == "uncertain" and reconcile is not None:
        reconciled = await reconcile()
        await ledger.mark_reconciled(effect_key, result=reconciled)
        return reconciled
    if not reservation.created:
        await ledger.mark_uncertain(
            effect_key,
            error="reserved effect replayed without a completed result",
        )
        raise RuntimeError(f"paid effect {effect_key!r} is ambiguous")

    try:
        result = await operation()
    except Exception as exc:
        if _definitely_not_billed(exc):
            await ledger.mark_failed(
                effect_key,
                error=type(exc).__name__,
                release_quota=True,
            )
        else:
            await ledger.mark_uncertain(effect_key, error=type(exc).__name__)
        raise
    await ledger.mark_succeeded(effect_key, result=result)
    return result


def _output_error(tool_name: str, expected_shape: str) -> ToolOutputError:
    return ToolOutputError(f"{tool_name} expected {expected_shape} from adapter")


def require_non_empty_string(value: Any, *, tool_name: str) -> str:
    expected = "non-empty str"
    if not isinstance(value, str) or not value.strip():
        raise _output_error(tool_name, expected)
    return value


def require_dict(value: Any, *, tool_name: str) -> dict[str, Any]:
    expected = "non-empty dict"
    if not isinstance(value, dict) or not value:
        raise _output_error(tool_name, expected)
    return value


def require_dict_list(value: Any, *, tool_name: str) -> list[dict[str, Any]]:
    expected = "non-empty list[dict[str, Any]]"
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, dict) or not item for item in value)
    ):
        raise _output_error(tool_name, expected)
    return value


def require_artifact(value: Any, *, tool_name: str) -> Artifact:
    expected = "Artifact with non-empty uri"
    if isinstance(value, Artifact):
        artifact = value
    elif isinstance(value, dict):
        try:
            artifact = Artifact.model_validate(value)
        except Exception as exc:  # noqa: BLE001 - adapter shape is untrusted
            raise _output_error(tool_name, expected) from exc
    else:
        raise _output_error(tool_name, expected)
    if not artifact.uri:
        raise _output_error(tool_name, expected)
    return artifact


def require_qc_result(value: Any, *, tool_name: str) -> QCResult:
    expected = "QCResult"
    if isinstance(value, QCResult):
        return value
    if isinstance(value, dict):
        try:
            return QCResult.model_validate(value)
        except Exception as exc:  # noqa: BLE001 - adapter shape is untrusted
            raise _output_error(tool_name, expected) from exc
    raise _output_error(tool_name, expected)
