"""Boundary between LangGraph nodes, native creative agents and typed tools."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from orchestrator.agent_catalog import (
    AgentCatalog,
    StageExecutionSpec,
    agent_stage_not_allowed_message,
    default_agent_catalog,
    is_agent_stage_allowed,
)
from orchestrator.language_runtime import agent_output_model
from orchestrator.tools.base import ToolContext
from orchestrator.tools.registry import get_tool_spec
from orchestrator.tracing import add_trace_metadata, traced


class StageExecutionError(RuntimeError):
    """Raised when a stage cannot execute through the configured catalog."""


ToolFn = Callable[..., Awaitable[Any]]


def _catalog_from_config(config: RunnableConfig) -> AgentCatalog:
    catalog = config.get("configurable", {}).get("agent_catalog")
    if catalog is None:
        return default_agent_catalog()
    if not isinstance(catalog, AgentCatalog):
        raise StageExecutionError("agent_catalog em configurable tem tipo inválido")
    return catalog


def _stage_spec(config: RunnableConfig, stage: str) -> StageExecutionSpec:
    try:
        return _catalog_from_config(config).stage(stage)
    except KeyError as exc:
        raise StageExecutionError(f"stage {stage!r} is not configured in agent_catalog") from exc


def _ensure_allowed(spec: StageExecutionSpec, tool_name: str) -> None:
    if tool_name not in spec.tools:
        raise StageExecutionError(f"tool {tool_name!r} is not allowed for stage {spec.stage!r}")


@traced("agent.stage_executor", run_type="chain")
async def _execute_agentic_tool(
    spec: StageExecutionSpec,
    ctx: ToolContext,
    tool_name: str,
    tool_fn: ToolFn,
    kwargs: dict[str, Any],
) -> Any:
    runtime = ctx.language_runtime
    if runtime is None:
        raise StageExecutionError(
            f"stage {spec.stage!r} executor=agent requires LanguageRuntime"
        )
    if not is_agent_stage_allowed(spec.stage):
        raise StageExecutionError(agent_stage_not_allowed_message())
    tool_spec = get_tool_spec(tool_name)
    output_model = agent_output_model(spec.stage)
    terminal_submission = tool_spec.terminal_submission and spec.schema_version == "creative-v2"
    add_trace_metadata(
        executor="langchain-agent",
        stage=spec.stage,
        tool_name=tool_name,
        target_model=spec.target_model,
        prompt_version=spec.prompt_version,
        prompt_hash=spec.prompt_hash,
        schema_version=spec.schema_version,
        run_id=ctx.run_id,
    )

    async def materialize(submission: dict[str, Any]) -> Any:
        if not isinstance(submission, dict):
            raise StageExecutionError("structured_response must be an object")
        try:
            validated = output_model.model_validate(submission)
        except ValidationError as exc:
            raise StageExecutionError(
                f"structured_response for stage {spec.stage!r} failed Pydantic validation"
            ) from exc
        safe = validated.model_dump(mode="json")
        trusted = {"agent_submission": True} if terminal_submission else {}
        return await tool_fn(ctx, **{**kwargs, **trusted, **safe})

    return await runtime.run_agent(
        stage=spec.stage,
        inputs=kwargs,
        system_prompt=spec.system_prompt,
        model=spec.target_model,
        materialize=materialize,
    )


async def execute_stage_tool(
    config: RunnableConfig,
    ctx: ToolContext,
    *,
    catalog_stage: str,
    tool_name: str,
    tool_fn: ToolFn,
    **kwargs: Any,
) -> Any:
    """Execute a typed tool directly or via the native LanguageRuntime agent."""
    spec = _stage_spec(config, catalog_stage)
    _ensure_allowed(spec, tool_name)
    if spec.executor == "tool":
        add_trace_metadata(executor="tool", stage=catalog_stage, tool_name=tool_name)
        return await tool_fn(ctx, **kwargs)
    if spec.executor != "agent":
        raise StageExecutionError(
            f"stage {catalog_stage!r} has invalid executor {spec.executor!r}"
        )
    if not spec.agent_enabled:
        raise StageExecutionError(
            f"stage {catalog_stage!r} executor: agent requires agent_enabled: true"
        )
    return await _execute_agentic_tool(spec, ctx, tool_name, tool_fn, kwargs)
