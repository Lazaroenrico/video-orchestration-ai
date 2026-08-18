from __future__ import annotations

import pytest
from langchain_core.runnables import RunnableConfig

from orchestrator.agent_catalog import AgentCatalog, StageExecutionSpec
from orchestrator.stage_executor import StageExecutionError, execute_stage_tool
from orchestrator.tools.base import ToolContext


class RecordingRuntime:
    def __init__(self, submission: dict[str, object] | object) -> None:
        self.submission = submission
        self.calls: list[dict[str, object]] = []

    async def generate_structured(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.submission


def _config(*, executor: str, stage: str = "concepts") -> RunnableConfig:
    catalog = AgentCatalog(
        stages=(
            StageExecutionSpec(
                stage=stage,
                executor=executor,
                tools=("generate_concepts",),
                agent_enabled=executor == "agent",
                schema_version="creative-v2" if executor == "agent" else None,
            ),
        )
    )
    return {
        "configurable": {
            "agent_catalog": catalog,
            "language_runtime": None,
        }
    }


def _context(runtime: object | None = None) -> ToolContext:
    return ToolContext(
        adapter=object(),
        pipeline={},
        run={"platform": "tiktok"},
        run_id="run-1",
        language_runtime=runtime,
    )


def _agent_submission() -> dict[str, object]:
    return {
        "proposals": [
            {
                "hook": "hook",
                "angle": "angle",
                "audience_problem": "problem",
                "product_mechanism": "mechanism",
                "evidence_basis": "cold_test",
                "format": "talking_head",
                "hook_style": "problem",
            }
        ]
    }


@pytest.mark.asyncio
async def test_agent_executor_fails_before_tool_when_runtime_is_missing() -> None:
    called = False

    async def tool(_ctx: ToolContext, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return "unexpected"

    with pytest.raises(StageExecutionError, match="requires LanguageRuntime"):
        await execute_stage_tool(
            _config(executor="agent"),
            _context(),
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=tool,
        )
    assert called is False


@pytest.mark.asyncio
async def test_native_agent_materializes_only_model_fields_and_marks_submission() -> None:
    runtime = RecordingRuntime(
        {
            "proposals": [
                {
                    "hook": "hook",
                    "angle": "angle",
                    "audience_problem": "problem",
                    "product_mechanism": "mechanism",
                    "evidence_basis": "cold_test",
                    "format": "talking_head",
                    "hook_style": "problem",
                }
            ],
        }
    )
    received: dict[str, object] = {}

    async def tool(_ctx: ToolContext, **kwargs: object) -> dict[str, object]:
        received.update(kwargs)
        return {"ok": True}

    result = await execute_stage_tool(
        _config(executor="agent"),
        _context(runtime),
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=tool,
        offer="offer",
    )

    assert result == {"ok": True}
    assert received == {
        "offer": "offer",
        "agent_submission": True,
        "proposals": [
            {
                "hook": "hook",
                "angle": "angle",
                "audience_problem": "problem",
                "product_mechanism": "mechanism",
                "evidence_basis": "cold_test",
                "format": "talking_head",
                "hook_style": "problem",
            }
        ],
    }
    assert runtime.calls[0]["stage"] == "concepts"


@pytest.mark.asyncio
async def test_native_agent_rejects_server_owned_fields_at_pydantic_boundary() -> None:
    runtime = RecordingRuntime(
        {
            "proposals": [
                {
                    "hook": "hook",
                    "angle": "angle",
                    "audience_problem": "problem",
                    "product_mechanism": "mechanism",
                    "evidence_basis": "cold_test",
                    "format": "talking_head",
                    "hook_style": "problem",
                }
            ],
            "concept_ids": ["server-owned"],
        }
    )

    async def tool(_ctx: ToolContext, **_kwargs: object) -> object:
        return object()

    with pytest.raises(StageExecutionError, match="Pydantic validation"):
        await execute_stage_tool(
            _config(executor="agent"),
            _context(runtime),
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=tool,
        )


@pytest.mark.asyncio
async def test_native_agent_rejects_non_object_submission() -> None:
    runtime = RecordingRuntime("invalid-string-output")

    async def tool(_ctx: ToolContext, **_kwargs: object) -> object:
        return object()

    with pytest.raises(StageExecutionError, match="structured_response must be an object"):
        await execute_stage_tool(
            _config(executor="agent"),
            _context(runtime),
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=tool,
        )


@pytest.mark.asyncio
async def test_native_agent_propagates_validation_error_from_runtime() -> None:
    from orchestrator.language_runtime import ConceptAgentOutput

    class FailingRuntime:
        async def generate_structured(self, **kwargs: object) -> object:
            # Trigger real validation error
            ConceptAgentOutput.model_validate({"proposals": "not-a-list"})

    with pytest.raises(StageExecutionError, match="Pydantic validation"):
        await execute_stage_tool(
            _config(executor="agent"),
            _context(FailingRuntime()),
            catalog_stage="concepts",
            tool_name="generate_concepts",
            tool_fn=lambda *_a, **_kw: None,
        )


@pytest.mark.asyncio
async def test_tool_executor_returns_domain_value_directly() -> None:
    artifact = object()

    async def tool(_ctx: ToolContext, **_kwargs: object) -> object:
        return artifact

    config = _config(executor="tool")
    result = await execute_stage_tool(
        config,
        _context(),
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=tool,
    )

    assert result is artifact


@pytest.mark.asyncio
async def test_agent_catalog_rejects_creative_agent_on_media_stage() -> None:
    catalog = AgentCatalog(
        stages=(
            StageExecutionSpec(
                stage="video",
                executor="agent",
                tools=("generate_concepts",),
                agent_enabled=True,
                schema_version="creative-v2",
            ),
        )
    )
    config: RunnableConfig = {"configurable": {"agent_catalog": catalog}}

    with pytest.raises(StageExecutionError, match="only supported for stages"):
        await execute_stage_tool(
            config,
            _context(RecordingRuntime({})),
            catalog_stage="video",
            tool_name="generate_concepts",
            tool_fn=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_stage_executor_rejects_tool_not_allowed_by_catalog() -> None:
    async def tool(_ctx: ToolContext, **_kwargs: object) -> str:
        return "unexpected"

    config = _config(executor="tool")
    config["configurable"]["agent_catalog"] = AgentCatalog(
        stages=(StageExecutionSpec(stage="concepts", executor="tool", tools=("other",)),)
    )
    with pytest.raises(StageExecutionError, match="not allowed"):
        await execute_stage_tool(
            config, _context(), catalog_stage="concepts", tool_name="generate_concepts", tool_fn=tool
        )


@pytest.mark.asyncio
async def test_stage_executor_rejects_stage_missing_from_catalog() -> None:
    async def tool(_ctx: ToolContext, **_kwargs: object) -> str:
        return "unexpected"

    config = {"configurable": {"agent_catalog": AgentCatalog(stages=())}}
    with pytest.raises(StageExecutionError, match="not configured"):
        await execute_stage_tool(
            config, _context(), catalog_stage="concepts", tool_name="generate_concepts", tool_fn=tool
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            StageExecutionSpec(
                stage="concepts", executor="agent", tools=("generate_concepts",), agent_enabled=False
            ),
            "requires agent_enabled",
        ),
        (
            StageExecutionSpec(stage="concepts", executor="worker", tools=("generate_concepts",)),
            "invalid executor",
        ),
    ],
)
async def test_stage_executor_rejects_invalid_catalog_specs(spec, message) -> None:
    async def tool(_ctx: ToolContext, **_kwargs: object) -> str:
        return "unexpected"

    config = {"configurable": {"agent_catalog": AgentCatalog(stages=(spec,))}}
    with pytest.raises(StageExecutionError, match=message):
        await execute_stage_tool(
            config, _context(), catalog_stage="concepts", tool_name="generate_concepts", tool_fn=tool
        )


@pytest.mark.asyncio
async def test_stage_executor_rejects_wrongly_typed_agent_catalog() -> None:
    async def tool(_ctx: ToolContext, **_kwargs: object) -> str:
        return "unexpected"

    config = {"configurable": {"agent_catalog": {"stages": {}}}}
    with pytest.raises(StageExecutionError, match="tipo inválido"):
        await execute_stage_tool(
            config, _context(), catalog_stage="concepts", tool_name="generate_concepts", tool_fn=tool
        )


@pytest.mark.asyncio
async def test_stage_executor_uses_default_catalog_when_absent() -> None:
    called = False

    async def tool(_ctx: ToolContext, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return "direct"

    config = {"configurable": {}}
    result = await execute_stage_tool(
        config, _context(), catalog_stage="concepts", tool_name="generate_concepts", tool_fn=tool
    )
    assert result == "direct"
    assert called is True


@pytest.mark.asyncio
async def test_agent_executor_emits_prompt_and_catalog_trace_metadata(monkeypatch) -> None:
    from orchestrator import stage_executor

    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(stage_executor, "add_trace_metadata", lambda **values: recorded.append(values))
    catalog = AgentCatalog(
        stages=(
            StageExecutionSpec(
                stage="concepts",
                executor="agent",
                tools=("generate_concepts",),
                agent_enabled=True,
                target_model="mock-model",
                prompt_version="concepts-v2",
                prompt_hash="hash",
                schema_version="creative-v2",
                system_prompt="guardrails",
            ),
        )
    )

    async def tool(_ctx: ToolContext, **_kwargs: object) -> str:
        return "ok"

    config = {"configurable": {"agent_catalog": catalog}}
    await execute_stage_tool(
        config,
        _context(RecordingRuntime(_agent_submission())),
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=tool,
    )
    assert recorded[0] == {
        "executor": "langchain-agent",
        "stage": "concepts",
        "tool_name": "generate_concepts",
        "target_model": "mock-model",
        "prompt_version": "concepts-v2",
        "prompt_hash": "hash",
        "schema_version": "creative-v2",
        "run_id": "run-1",
    }
