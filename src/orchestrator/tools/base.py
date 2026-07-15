"""Shared primitives for the thin node -> tool -> adapter layer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


def tool_context_from_config(config: RunnableConfig) -> ToolContext:
    """Extract the already-resolved adapter and runtime knobs from RunnableConfig."""
    configurable = config["configurable"]
    return ToolContext(
        adapter=configurable["adapter"],
        pipeline=configurable.get("pipeline", {}),
        run=configurable.get("run", {}),
        run_id=configurable.get("thread_id", "run"),
    )


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
