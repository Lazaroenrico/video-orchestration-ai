"""Configurable stage executor for the node -> tool boundary."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig

from orchestrator.agent_catalog import (
    AgentCatalog,
    StageExecutionSpec,
    agent_stage_not_allowed_message,
    default_agent_catalog,
    is_agent_stage_allowed,
)
from orchestrator.tools.base import ToolContext
from orchestrator.tracing import add_trace_metadata, traced


class StageExecutionError(RuntimeError):
    """Raised when a stage cannot execute through the configured catalog."""


ToolFn = Callable[..., Awaitable[Any]]


def _catalog_from_config(config: RunnableConfig) -> AgentCatalog:
    catalog = config.get("configurable", {}).get("agent_catalog")
    if catalog is None:
        return default_agent_catalog()
    if not isinstance(catalog, AgentCatalog):
        raise StageExecutionError(
            f"agent_catalog em configurable tem tipo inválido: {type(catalog).__name__} "
            "(esperado AgentCatalog)"
        )
    return catalog


def _stage_spec(config: RunnableConfig, stage: str) -> StageExecutionSpec:
    try:
        return _catalog_from_config(config).stage(stage)
    except KeyError as exc:
        raise StageExecutionError(f"stage {stage!r} is not configured in agent_catalog") from exc


def _ensure_allowed(spec: StageExecutionSpec, tool_name: str) -> None:
    if tool_name not in spec.tools:
        raise StageExecutionError(
            f"tool {tool_name!r} is not allowed for stage {spec.stage!r}"
        )


@traced("agent.stage_executor", run_type="chain")
async def _execute_agentic_tool(
    spec: StageExecutionSpec,
    ctx: ToolContext,
    tool_name: str,
    tool_fn: ToolFn,
    kwargs: dict[str, Any],
) -> Any:
    add_trace_metadata(
        executor="agent",
        stage=spec.stage,
        tool_name=tool_name,
        target_model=spec.target_model,
        target_agent=spec.target_agent,
        allowed_tools=list(spec.tools),
        run_id=ctx.run_id,
    )
    # O agent brain é um port opcional do adapter llm (Fase 7). Ausente → passthrough
    # puro (a tool roda uma vez), preservando o comportamento offline sem custo.
    run_stage_agent = getattr(ctx.adapter, "run_stage_agent", None)
    if run_stage_agent is None:
        add_trace_metadata(agent_backend="passthrough")
        return await tool_fn(ctx, **kwargs)

    async def run_tool(**tool_inputs: Any) -> Any:
        # Fronteira D29: o agent só toca o domínio via a typed tool (validada).
        return await tool_fn(ctx, **tool_inputs)

    return await run_stage_agent(
        stage=spec.stage,
        allowed_tools=spec.tools,
        run_tool=run_tool,
        inputs=kwargs,
        target_model=spec.target_model,
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
    if not is_agent_stage_allowed(spec.stage):
        raise StageExecutionError(agent_stage_not_allowed_message())
    return await _execute_agentic_tool(spec, ctx, tool_name, tool_fn, kwargs)
