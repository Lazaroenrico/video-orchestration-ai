"""Stage executor contract for D29 phases 4-5."""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.agent_catalog import AgentCatalog, StageExecutionSpec
from orchestrator.tools.base import ToolContext


class _Adapter:
    async def generate_concepts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "concept-1", "hook": kwargs["offer"]}]


def _config(catalog: AgentCatalog) -> dict[str, Any]:
    return {
        "configurable": {
            "adapter": _Adapter(),
            "pipeline": {},
            "run": {},
            "thread_id": "stage-executor-test",
            "agent_catalog": catalog,
        }
    }


def _catalog(*, executor: str = "tool", tools: tuple[str, ...] = ("generate_concepts",)) -> AgentCatalog:
    return AgentCatalog(
        stages=(
            StageExecutionSpec(
                stage="concepts",
                executor=executor,
                tools=tools,
                agent_enabled=executor == "agent",
                target_model="mock-model" if executor == "agent" else None,
                target_agent="concept-agent" if executor == "agent" else None,
            ),
        )
    )


async def test_stage_executor_tool_mode_calls_the_tool_directly():
    from orchestrator.stage_executor import execute_stage_tool
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.concepts import generate_concepts_tool

    cfg = _config(_catalog())
    ctx = tool_context_from_config(cfg)

    result = await execute_stage_tool(
        cfg,
        ctx,
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=generate_concepts_tool,
        offer="serum",
        n=1,
        seed="run",
        bias=None,
    )

    assert result == [{"id": "concept-1", "hook": "serum"}]


async def test_stage_executor_agent_mode_still_returns_validated_tool_output():
    from orchestrator.stage_executor import execute_stage_tool
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.concepts import generate_concepts_tool

    cfg = _config(_catalog(executor="agent"))
    ctx = tool_context_from_config(cfg)

    result = await execute_stage_tool(
        cfg,
        ctx,
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=generate_concepts_tool,
        offer="agent serum",
        n=1,
        seed="run",
        bias=None,
    )

    assert result == [{"id": "concept-1", "hook": "agent serum"}]


async def test_stage_executor_agent_mode_emits_agent_trace_span_metadata(monkeypatch):
    """O modo agent adiciona seu span próprio com metadata de roteamento.

    Aqui o adapter (``_Adapter``) não implementa ``run_stage_agent``, então o executor
    cai no passthrough — mas ainda emite a metadata do span do agent
    (``executor="agent"``, ``allowed_tools``, ``target_model``/``target_agent``) seguida
    de ``agent_backend="passthrough"``. Isso separa o modo agent do tool mesmo sem
    backend agentic.
    """
    from orchestrator import stage_executor
    from orchestrator.stage_executor import execute_stage_tool
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.concepts import generate_concepts_tool

    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stage_executor, "add_trace_metadata", lambda **kw: recorded.append(kw)
    )

    cfg = _config(_catalog(executor="agent"))
    ctx = tool_context_from_config(cfg)

    result = await execute_stage_tool(
        cfg,
        ctx,
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=generate_concepts_tool,
        offer="serum",
        n=1,
        seed="run",
        bias=None,
    )

    assert result == [{"id": "concept-1", "hook": "serum"}]
    assert recorded == [
        {
            "executor": "agent",
            "stage": "concepts",
            "tool_name": "generate_concepts",
            "target_model": "mock-model",
            "target_agent": "concept-agent",
            "allowed_tools": ["generate_concepts"],
            "run_id": ctx.run_id,
        },
        {"agent_backend": "passthrough"},
    ]


async def test_stage_executor_tool_mode_emits_only_tool_trace_metadata(monkeypatch):
    """Espelho do teste agent: o modo tool emite ``executor="tool"`` e nada de agent."""
    from orchestrator import stage_executor
    from orchestrator.stage_executor import execute_stage_tool
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.concepts import generate_concepts_tool

    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        stage_executor, "add_trace_metadata", lambda **kw: recorded.append(kw)
    )

    cfg = _config(_catalog())  # executor="tool"
    ctx = tool_context_from_config(cfg)

    await execute_stage_tool(
        cfg,
        ctx,
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=generate_concepts_tool,
        offer="serum",
        n=1,
        seed="run",
        bias=None,
    )

    assert recorded == [
        {"executor": "tool", "stage": "concepts", "tool_name": "generate_concepts"}
    ]


def test_agentic_executor_declares_dedicated_trace_span():
    """Trava a promessa de observabilidade do ADR: o agent execution é um span próprio.

    Espelha os testes de trace-marker das tools em ``test_tools.py`` — se o span
    ``agent.stage_executor`` for renomeado/removido, isto falha.
    """
    from orchestrator.stage_executor import _execute_agentic_tool

    assert getattr(_execute_agentic_tool, "__trace_name__") == "agent.stage_executor"
    assert getattr(_execute_agentic_tool, "__trace_run_type__") == "chain"


async def test_stage_executor_rejects_tool_not_allowed_by_catalog():
    from orchestrator.stage_executor import StageExecutionError, execute_stage_tool

    async def fake_tool(ctx: ToolContext, **kwargs: Any) -> str:
        return "should not run"

    cfg = _config(_catalog(tools=("other_tool",)))
    ctx = ToolContext(adapter=object(), pipeline={}, run={}, run_id="run")

    with pytest.raises(StageExecutionError, match="not allowed"):
        await execute_stage_tool(
            cfg,
            ctx,
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=fake_tool,
        )


async def test_stage_executor_rejects_stage_missing_from_catalog():
    from orchestrator.stage_executor import StageExecutionError, execute_stage_tool

    async def fake_tool(ctx: ToolContext, **kwargs: Any) -> str:
        return "should not run"

    cfg = _config(AgentCatalog(stages=()))
    ctx = ToolContext(adapter=object(), pipeline={}, run={}, run_id="run")

    with pytest.raises(StageExecutionError, match="not configured"):
        await execute_stage_tool(
            cfg,
            ctx,
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=fake_tool,
        )


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            StageExecutionSpec(
                stage="concepts",
                executor="agent",
                tools=("generate_concepts",),
                agent_enabled=False,
            ),
            "requires agent_enabled",
        ),
        (
            StageExecutionSpec(
                stage="concepts",
                executor="worker",
                tools=("generate_concepts",),
            ),
            "invalid executor",
        ),
    ],
)
async def test_stage_executor_rejects_invalid_manual_catalog_specs(spec, message):
    from orchestrator.stage_executor import StageExecutionError, execute_stage_tool

    async def fake_tool(ctx: ToolContext, **kwargs: Any) -> str:
        return "should not run"

    cfg = _config(AgentCatalog(stages=(spec,)))
    ctx = ToolContext(adapter=object(), pipeline={}, run={}, run_id="run")

    with pytest.raises(StageExecutionError, match=message):
        await execute_stage_tool(
            cfg,
            ctx,
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=fake_tool,
        )


async def test_stage_executor_rejects_wrongly_typed_agent_catalog():
    """A1: um agent_catalog presente mas mal-tipado (ex.: dict de as_dict()) nao pode
    cair silenciosamente no default tool-mode — deve estourar alto."""
    from orchestrator.stage_executor import StageExecutionError, execute_stage_tool

    async def fake_tool(ctx: ToolContext, **kwargs: Any) -> str:
        return "should not run"

    cfg = {"configurable": {"agent_catalog": {"stages": {}}}}  # dict, nao AgentCatalog
    ctx = ToolContext(adapter=object(), pipeline={}, run={}, run_id="run")

    with pytest.raises(StageExecutionError, match="tipo inválido"):
        await execute_stage_tool(
            cfg,
            ctx,
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=fake_tool,
        )


async def test_stage_executor_uses_default_catalog_when_absent():
    """A1: sem agent_catalog em configurable, cai no default (tool mode) e roda a tool."""
    from orchestrator.stage_executor import execute_stage_tool
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.concepts import generate_concepts_tool

    cfg = {"configurable": {"adapter": _Adapter(), "pipeline": {}, "run": {}, "thread_id": "t"}}
    ctx = tool_context_from_config(cfg)

    result = await execute_stage_tool(
        cfg,
        ctx,
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=generate_concepts_tool,
        offer="serum",
        n=1,
        seed="run",
        bias=None,
    )

    assert result == [{"id": "concept-1", "hook": "serum"}]


async def test_stage_executor_blocks_agent_mode_on_media_stage_at_runtime():
    """A2: o gate 'agent so em concepts/scripts' vale no executor, nao so no load do YAML.

    Um AgentCatalog construido programaticamente com um media stage em modo agent
    deve ser rejeitado em runtime pelo executor.
    """
    from orchestrator.stage_executor import StageExecutionError, execute_stage_tool

    async def fake_tool(ctx: ToolContext, **kwargs: Any) -> str:
        return "should not run"

    media_catalog = AgentCatalog(
        stages=(
            StageExecutionSpec(
                stage="video",
                executor="agent",
                tools=("generate_clip",),
                agent_enabled=True,
                target_model="mock-model",
            ),
        )
    )
    cfg = _config(media_catalog)
    ctx = ToolContext(adapter=object(), pipeline={}, run={}, run_id="run")

    with pytest.raises(StageExecutionError, match="only supported for stages"):
        await execute_stage_tool(
            cfg,
            ctx,
            catalog_stage="video",
            tool_name="generate_clip",
            tool_fn=fake_tool,
        )


async def test_mock_pipeline_can_opt_into_agentic_concepts_and_scripts(tmp_path, pipeline_cfg):
    from orchestrator.agent_catalog import build_agent_catalog
    from orchestrator.runner import run_pipeline, summarize

    catalog = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "agent",
                    "tools": ["generate_concepts"],
                    "target_model": "mock-model",
                    "target_agent": "concept-agent",
                    "agent_enabled": True,
                },
                "scripts": {
                    "executor": "agent",
                    "tools": ["write_script"],
                    "target_model": "mock-model",
                    "target_agent": "script-agent",
                    "agent_enabled": True,
                },
            }
        }
    )

    run_id, out = await run_pipeline(
        pipeline_cfg,
        {"adapters": {}},
        db_path=tmp_path / "runs.sqlite",
        run_id="agentic-pilot",
        batch=2,
        offer="serum X",
        agent_catalog=catalog,
    )

    summary = summarize({**out, "run_id": run_id})
    assert summary["run_id"] == "agentic-pilot"
    assert summary["produced"] == 2
    assert all(item.script for item in out["results"])


async def test_mock_run_stage_agent_meets_acceptance_criteria():
    """Critério de aceite D31 (mock): critique aceita → 1 chamada de tool, retorna o
    rascunho; critique pede refino → 2 chamadas (2ª com ``revision``) e output difere."""
    from orchestrator.adapters.mock import MockAdapter

    adapter = MockAdapter(tiers=[])

    # O critique do mock é determinístico (hash do rascunho). Acha um rascunho que
    # aprova e outro que pede refino, sem hardcodar valores mágicos.
    approve_draft = refine_draft = None
    i = 0
    while approve_draft is None or refine_draft is None:
        candidate = [f"draft-{i}"]
        if MockAdapter._agent_critique("concepts", candidate) is None:
            approve_draft = approve_draft or candidate
        else:
            refine_draft = refine_draft or candidate
        i += 1

    # Aceita: 1 chamada, retorna o rascunho intacto.
    approve_calls: list[dict[str, Any]] = []

    async def approve_tool(**inputs: Any) -> Any:
        approve_calls.append(inputs)
        return approve_draft

    approved = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=approve_tool,
        inputs={"offer": "o"},
    )
    assert len(approve_calls) == 1
    assert "revision" not in approve_calls[0]
    assert approved == approve_draft

    # Refina: 2 chamadas, a 2ª com ``revision``, e o output difere do rascunho.
    refine_calls: list[dict[str, Any]] = []

    async def refine_tool(**inputs: Any) -> Any:
        refine_calls.append(inputs)
        if "revision" in inputs:
            return refine_draft + ["REFINED"]
        return refine_draft

    refined = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=refine_tool,
        inputs={"offer": "o"},
    )
    assert len(refine_calls) == 2
    assert "revision" in refine_calls[1]
    assert refined != refine_draft
