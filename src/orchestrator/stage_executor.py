"""Configurable stage executor for the node -> tool boundary."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig

from orchestrator.adapters._agent_loop import (
    DEFAULT_MAX_STEPS,
    AgentRunResult,
    ToolAttempt,
    ToolCall,
)
from orchestrator.agent_catalog import (
    AgentCatalog,
    StageExecutionSpec,
    agent_stage_not_allowed_message,
    default_agent_catalog,
    is_agent_stage_allowed,
)
from orchestrator.tools.base import ToolContext
from orchestrator.tools.registry import get_tool_spec
from orchestrator.tracing import add_trace_metadata, traced


class StageExecutionError(RuntimeError):
    """Raised when a stage cannot execute through the configured catalog."""


ToolFn = Callable[..., Awaitable[Any]]


def _positive_int(raw: Any) -> int | None:
    """Um int > 0, ou None. ``bool`` é rejeitado: ``isinstance(True, int)`` é True."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return None


def _agent_cfg(pipeline: dict[str, Any]) -> dict[str, Any]:
    agent_cfg = pipeline.get("agent") if isinstance(pipeline, dict) else None
    return agent_cfg if isinstance(agent_cfg, dict) else {}


def _by_stage(agent_cfg: dict[str, Any], key: str, stage: str) -> int | None:
    by_stage = agent_cfg.get(key)
    return _positive_int(by_stage.get(stage)) if isinstance(by_stage, dict) else None


def _agent_max_steps(pipeline: dict[str, Any], stage: str) -> int:
    """Budget de rodadas: ``max_steps_by_stage.<stage>`` > ``max_steps`` > default.

    Por stage porque o custo por rodada varia em ordens de grandeza entre texto
    (centavos) e vídeo (dólares por take) — D33.
    """
    agent_cfg = _agent_cfg(pipeline)
    return (
        _by_stage(agent_cfg, "max_steps_by_stage", stage)
        or _positive_int(agent_cfg.get("max_steps"))
        or DEFAULT_MAX_STEPS
    )


def _agent_max_tool_calls(pipeline: dict[str, Any], stage: str) -> int | None:
    """Cap de chamadas de tool (``None`` = sem cap).

    ``max_steps`` conta rodadas do modelo, não tool calls: um único step pode pedir N
    takes. Em mídia cada take é dinheiro, então este é o único teto de custo real (D33).
    """
    agent_cfg = _agent_cfg(pipeline)
    return _by_stage(agent_cfg, "max_tool_calls_by_stage", stage) or _positive_int(
        agent_cfg.get("max_tool_calls")
    )


def _direct_run(tool_name: str, result: Any) -> AgentRunResult:
    """Embrulha uma execução direta (modo tool / passthrough) como uma tentativa única.

    Mantém o retorno de ``with_attempts=True`` simétrico entre tool, passthrough e agent,
    para o node de mídia ter um só caminho de contabilidade.
    """
    call = ToolCall(id="direct", name=tool_name)
    return AgentRunResult(result=result, attempts=(ToolAttempt(call=call, result=result),))


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
) -> AgentRunResult:
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
        return _direct_run(tool_name, await tool_fn(ctx, **kwargs))

    async def run_tool(called_tool: str, **tool_inputs: Any) -> Any:
        # Fronteira D29: o agent só toca o domínio via a typed tool (validada) e só
        # pela tool do stage. Fase 1 = uma tool por stage (a primária, ``tool_fn``);
        # multi-tool por stage entra na Fase 2 (resolução por nome via registry).
        if called_tool != tool_name:
            raise StageExecutionError(
                f"tool {called_tool!r} is not allowed for stage {spec.stage!r}"
            )
        # Server-authoritative: o modelo só influencia os params declarados no schema
        # da tool (ex.: ``revision``); offer/n/seed/etc. vêm dos inputs confiáveis.
        allowed_params = get_tool_spec(called_tool).parameters.get("properties", {})
        safe_inputs = {k: v for k, v in tool_inputs.items() if k in allowed_params}
        return await tool_fn(ctx, **{**kwargs, **safe_inputs})

    return await run_stage_agent(
        stage=spec.stage,
        allowed_tools=spec.tools,
        run_tool=run_tool,
        inputs=kwargs,
        target_model=spec.target_model,
        max_steps=_agent_max_steps(ctx.pipeline, spec.stage),
        max_tool_calls=_agent_max_tool_calls(ctx.pipeline, spec.stage),
    )


async def execute_stage_tool(
    config: RunnableConfig,
    ctx: ToolContext,
    *,
    catalog_stage: str,
    tool_name: str,
    tool_fn: ToolFn,
    with_attempts: bool = False,
    **kwargs: Any,
) -> Any:
    """Roda a tool do stage pelo executor configurado (tool direto ou agent).

    ``with_attempts=False`` (default) devolve o output de domínio cru — o contrato que
    os nodes não-agentic esperam. ``with_attempts=True`` devolve sempre um
    ``AgentRunResult``, inclusive em modo tool e no passthrough, para o node ter um só
    caminho de contabilidade de custo por tentativa (D33).
    """
    spec = _stage_spec(config, catalog_stage)
    _ensure_allowed(spec, tool_name)
    if spec.executor == "tool":
        add_trace_metadata(executor="tool", stage=catalog_stage, tool_name=tool_name)
        result = await tool_fn(ctx, **kwargs)
        return _direct_run(tool_name, result) if with_attempts else result
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
    run = await _execute_agentic_tool(spec, ctx, tool_name, tool_fn, kwargs)
    return run if with_attempts else run.result
