"""Contrato observável do runtime de linguagem nativo."""

import pytest


def test_mock_language_runtime_is_deterministic_and_offline():
    from orchestrator.language_runtime import LanguageRuntime

    runtime = LanguageRuntime.from_provider("mock", {})
    first = runtime.model_for("concepts").invoke("same input")
    second = runtime.model_for("concepts").invoke("same input")

    assert first.content == second.content
    assert runtime.provider == "mock"


@pytest.mark.asyncio
async def test_mock_agent_generate_structured_returns_pydantic_model():
    from pydantic import BaseModel

    from orchestrator.language_runtime import ConceptAgentOutput, LanguageRuntime

    runtime = LanguageRuntime.from_provider("mock", {})

    result = await runtime.generate_structured(
        stage="concepts",
        inputs={"offer": "offer", "n": 2, "campaign": {"offer": "offer", "batch_size": 2}},
    )

    assert isinstance(result, BaseModel)
    assert isinstance(result, ConceptAgentOutput)
    assert len(result.proposals) == 2


def test_live_language_provider_fails_before_model_construction_without_credentials(monkeypatch):
    from orchestrator.language_runtime import LanguageRuntime

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    runtime = LanguageRuntime.from_provider("vercel_gateway_llm", {})

    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        runtime.model_for("concepts")


def test_gateway_models_use_provider_specific_base_urls_and_retry_policy(monkeypatch):
    from orchestrator import language_runtime

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gateway-secret")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", FakeOpenAI)
    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", FakeAnthropic)

    openai_model = language_runtime.LanguageRuntime.from_provider(
        "vercel_gateway_llm", {}
    ).model_for("concepts", "openai-model")
    anthropic_model = language_runtime.LanguageRuntime.from_provider(
        "anthropic_sdk_gateway", {}
    ).model_for("concepts", "anthropic-model")

    assert openai_model.kwargs == {
        "model": "openai-model",
        "api_key": "gateway-secret",
        "base_url": "https://gateway.example/v1",
        "timeout": 120,
        "max_retries": 3,
    }
    assert anthropic_model.kwargs == {
        "model": "anthropic-model",
        "api_key": "gateway-secret",
        "base_url": "https://gateway.example",
        "timeout": 120,
        "max_retries": 4,
    }


def test_native_agents_are_limited_to_creative_stages():
    from orchestrator.language_runtime import LanguageRuntime

    runtime = LanguageRuntime.from_provider("mock", {})
    with pytest.raises(ValueError, match="only supported"):
        runtime.agent_for("video")


@pytest.mark.asyncio
async def test_generate_structured_rejects_non_creative_stage():
    from orchestrator.language_runtime import LanguageRuntime

    runtime = LanguageRuntime.from_provider("mock", {})
    with pytest.raises(ValueError, match="only supported"):
        await runtime.generate_structured(stage="video", inputs={})


@pytest.mark.asyncio
async def test_generate_structured_handles_dict_and_error_modes(monkeypatch):
    from orchestrator.language_runtime import ConceptAgentOutput, LanguageRuntime

    class FakeAgent:
        def __init__(self, response):
            self.response = response

        async def ainvoke(self, _inputs):
            return self.response

    runtime = LanguageRuntime.from_provider("anthropic", {"llm_model": "test-model"})

    # 1. Dict response gets converted to Pydantic model
    valid_dict = {
        "structured_response": {
            "proposals": [
                {
                    "hook": "h",
                    "angle": "a",
                    "audience_problem": "p",
                    "product_mechanism": "m",
                    "evidence_basis": "cold_test",
                    "format": "f",
                    "hook_style": "s",
                }
            ]
        }
    }
    monkeypatch.setattr(runtime, "agent_for", lambda *a, **kw: FakeAgent(valid_dict))
    res = await runtime.generate_structured(stage="concepts", inputs={"offer": "x"})
    assert isinstance(res, ConceptAgentOutput)
    assert res.proposals[0].hook == "h"

    # 2. Missing structured_response raises RuntimeError
    monkeypatch.setattr(runtime, "agent_for", lambda *a, **kw: FakeAgent({"messages": []}))
    with pytest.raises(RuntimeError, match="did not return structured_response"):
        await runtime.generate_structured(stage="concepts", inputs={"offer": "x"})

    # 3. Unexpected type raises RuntimeError
    monkeypatch.setattr(runtime, "agent_for", lambda *a, **kw: FakeAgent({"structured_response": 12345}))
    with pytest.raises(RuntimeError, match="unexpected structured_response type"):
        await runtime.generate_structured(stage="concepts", inputs={"offer": "x"})


def test_agent_budgets_use_pipeline_overrides_and_are_not_silenced(monkeypatch):
    from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

    from orchestrator.language_runtime import LanguageRuntime

    captured: list[dict[str, object]] = []

    def fake_create_agent(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr("langchain.agents.create_agent", fake_create_agent)
    runtime = LanguageRuntime.from_provider(
        "mock",
        {
            "agent": {
                "max_steps": 2,
                "max_tool_calls": 1,
                "max_steps_by_stage": {"scripts": 3},
                "max_tool_calls_by_stage": {"scripts": 2},
            }
        },
    )

    runtime.agent_for("scripts", system_prompt="scripts-v1")
    runtime.agent_for("concepts", system_prompt="concepts-v1")

    scripts_limits = captured[0]["middleware"]
    concepts_limits = captured[1]["middleware"]
    assert isinstance(scripts_limits[0], ModelCallLimitMiddleware)
    assert scripts_limits[0].run_limit == 3
    assert isinstance(scripts_limits[1], ToolCallLimitMiddleware)
    assert scripts_limits[1].run_limit == 2
    assert scripts_limits[0].exit_behavior == "error"
    assert scripts_limits[1].exit_behavior == "error"
    assert concepts_limits[0].run_limit == 2
    assert concepts_limits[1].run_limit == 1
    assert type(captured[0]["model"]).__name__ == "_SerialToolCallsChatModel"
    assert captured[0]["model"].wrapped is runtime.model_for("scripts")

    runtime.agent_for("scripts", system_prompt="scripts-v2")
    assert len(captured) == 3

    uncapped_tools = LanguageRuntime.from_provider(
        "mock", {"agent": {"max_steps": 5}}
    )
    uncapped_tools.agent_for("concepts", system_prompt="concepts-v1")
    assert len(captured[3]["middleware"]) == 1
    assert captured[3]["middleware"][0].run_limit == 5


@pytest.mark.asyncio
async def test_native_agent_keeps_base_model_and_binds_serial_structured_output():
    """Exercise the real create_agent seam used by live creative stages."""
    import asyncio
    from typing import Any, ClassVar

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from orchestrator.language_runtime import LanguageRuntime

    class FakeStructuredModel(BaseChatModel):
        bind_calls: ClassVar[list[dict[str, Any]]] = []
        bound_tools: ClassVar[list[Any]] = []

        @property
        def _llm_type(self) -> str:
            return "fake-structured"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            type(self).bind_calls.append(dict(kwargs))
            type(self).bound_tools = list(tools)
            return self.bind(**kwargs)

        def _generate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            del messages, stop, kwargs
            tool = type(self).bound_tools[0]
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": tool.name,
                                    "args": {
                                        "proposals": [
                                            {
                                                "hook": "hook",
                                                "angle": "angle",
                                                "audience_problem": "problem",
                                                "product_mechanism": "mechanism",
                                                "evidence_basis": "provided_fact",
                                                "format": "talking_head",
                                                "hook_style": "problem",
                                            }
                                        ]
                                    },
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )

        async def _agenerate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            return self._generate(messages, stop=stop, **kwargs)

    FakeStructuredModel.bind_calls = []
    FakeStructuredModel.bound_tools = []
    runtime = LanguageRuntime.from_provider("mock", {"agent": {"max_steps": 2}})
    model = FakeStructuredModel()
    runtime._models["mock"] = model
    agent = runtime.agent_for("concepts", system_prompt="safe system prompt")
    result = await asyncio.wait_for(
        agent.ainvoke({"messages": [HumanMessage(content="untrusted input")]}),
        timeout=3,
    )

    assert result["structured_response"].proposals[0].hook == "hook"
    assert FakeStructuredModel.bind_calls
    assert all(
        call.get("parallel_tool_calls") is False
        for call in FakeStructuredModel.bind_calls
    )


def test_language_model_factory_resolves_mock_deployment():
    from orchestrator.language_runtime import LanguageModelFactory, MockChatModel

    factory = LanguageModelFactory("mock")
    model = factory.create_model()
    assert isinstance(model, MockChatModel)
    assert model.model == "mock"
    assert factory.resolve_model_name() == "mock"
    assert factory.resolve_model_name("custom-mock") == "custom-mock"


def test_language_model_factory_resolve_model_name_accepts_override_keyword():
    from orchestrator.language_runtime import LanguageModelFactory

    factory = LanguageModelFactory("mock")

    assert factory.resolve_model_name(override="custom-mock") == "custom-mock"


def test_language_model_factory_create_model_accepts_model_keyword():
    from orchestrator.language_runtime import LanguageModelFactory, MockChatModel

    model = LanguageModelFactory("mock").create_model(model="custom-mock")

    assert isinstance(model, MockChatModel)
    assert model.model == "custom-mock"


def test_language_model_factory_rejects_arbitrary_or_unknown_provider():
    from orchestrator.language_runtime import LanguageModelFactory

    with pytest.raises(KeyError, match="desconhecido"):
        LanguageModelFactory("untrusted_provider").create_model()

    with pytest.raises(KeyError, match="desconhecido"):
        LanguageModelFactory.resolve_model_name("arbitrary_user_provider")

    with pytest.raises(KeyError, match="desconhecido"):
        LanguageModelFactory.create_model("attacker_supplied_provider")


def test_language_model_factory_auth_precedence_and_missing_credentials(monkeypatch):
    from orchestrator.language_runtime import LanguageModelFactory

    # vercel_gateway_llm without keys
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY or VERCEL_OIDC_TOKEN"):
        LanguageModelFactory("vercel_gateway_llm").create_model()

    # anthropic without keys
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        LanguageModelFactory("anthropic").create_model()

    # anthropic_sdk_gateway without keys
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY or VERCEL_OIDC_TOKEN"):
        LanguageModelFactory("anthropic_sdk_gateway").create_model()


def test_language_model_factory_init_chat_model_deployments(monkeypatch):
    from orchestrator.language_runtime import LanguageModelFactory

    captured_inits: list[dict[str, object]] = []

    def fake_init_chat_model(model: str, **kwargs: object):
        captured_inits.append({"model": model, **kwargs})
        return object()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)

    # 1. vercel_gateway_llm with VERCEL_OIDC_TOKEN
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token-123")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://gateway.internal")
    LanguageModelFactory("vercel_gateway_llm", {"llm_model": "openai/gpt-5"}).create_model()

    assert captured_inits[-1] == {
        "model": "openai/gpt-5",
        "model_provider": "openai",
        "api_key": "oidc-token-123",
        "base_url": "https://gateway.internal/v1",
        "timeout": 120,
        "max_retries": 3,
    }

    # 2. AI_GATEWAY_API_KEY takes precedence over VERCEL_OIDC_TOKEN
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "primary-api-key")
    LanguageModelFactory("vercel_gateway_llm").create_model("custom/model-override")
    assert captured_inits[-1]["api_key"] == "primary-api-key"
    assert captured_inits[-1]["model"] == "custom/model-override"

    # 3. anthropic direct
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    LanguageModelFactory("anthropic").create_model()
    assert captured_inits[-1] == {
        "model": "claude-opus-4-8",
        "model_provider": "anthropic",
        "api_key": "anthropic-secret",
        "timeout": 120,
        "max_retries": 3,
    }

    # 4. anthropic_sdk_gateway strips /v1
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh/v1")
    LanguageModelFactory("anthropic_sdk_gateway").create_model()
    assert captured_inits[-1] == {
        "model": "anthropic/claude-opus-4.8",
        "model_provider": "anthropic",
        "api_key": "primary-api-key",
        "base_url": "https://ai-gateway.vercel.sh",
        "timeout": 120,
        "max_retries": 4,
    }


def test_language_model_factory_enforces_trace_redaction(monkeypatch):
    from orchestrator.language_runtime import LanguageModelFactory

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_HIDE_INPUTS", "false")
    monkeypatch.setenv("LANGSMITH_HIDE_OUTPUTS", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

    with pytest.raises(RuntimeError, match="LANGSMITH_HIDE_INPUTS=true and LANGSMITH_HIDE_OUTPUTS=true"):
        LanguageModelFactory("anthropic").create_model()

    # With redaction properly enabled, it succeeds
    monkeypatch.setenv("LANGSMITH_HIDE_INPUTS", "true")
    monkeypatch.setenv("LANGSMITH_HIDE_OUTPUTS", "true")
    captured = []
    monkeypatch.setattr("langchain.chat_models.init_chat_model", lambda *a, **kw: captured.append(kw) or object())
    LanguageModelFactory("anthropic").create_model()
    assert len(captured) == 1



@pytest.mark.asyncio
async def test_mock_language_runtime_generate_concepts_determinism_and_spread():
    from orchestrator.language_runtime import LanguageRuntime

    runtime = LanguageRuntime.from_provider("mock", {})
    a = await runtime.generate_concepts(offer="serum X", n=10, seed="wk1")
    b = await runtime.generate_concepts(offer="serum X", n=10, seed="wk1")

    assert len(a) == 10
    assert a == b
    assert len({c["hook_style"] for c in a}) > 1


@pytest.mark.asyncio
async def test_mock_language_runtime_generate_concepts_seed_changes_output():
    from orchestrator.language_runtime import LanguageRuntime

    runtime = LanguageRuntime.from_provider("mock", {})
    a = await runtime.generate_concepts(offer="serum X", n=5, seed="wk1")
    b = await runtime.generate_concepts(offer="serum X", n=5, seed="wk2")

    assert a != b


@pytest.mark.asyncio
async def test_mock_language_runtime_write_script_has_hook_and_cta():
    from orchestrator.language_runtime import LanguageRuntime

    runtime = LanguageRuntime.from_provider("mock", {})
    concept = {"id": "concept-1", "hook": "você está fazendo errado", "angle": "problema", "hook_style": "problem", "offer": "o serum"}
    script = await runtime.write_script(concept=concept, creator_ref="creator-1", platform="tiktok")

    assert isinstance(script, str)
    assert "HOOK" in script.upper()
    assert "CTA" in script.upper()
    assert "tiktok" in script.lower()


@pytest.mark.asyncio
async def test_mock_language_runtime_generate_structured_all_stages():
    from orchestrator.language_runtime import (
        ConceptAgentOutput,
        CreatorProfilesAgentOutput,
        LanguageRuntime,
        ScriptAgentOutput,
    )

    runtime = LanguageRuntime.from_provider("mock", {})

    concepts_out = await runtime.generate_structured(
        stage="concepts",
        inputs={"offer": "Serum Y", "n": 3, "campaign": {"offer": "Serum Y", "batch_size": 3}},
    )
    assert isinstance(concepts_out, ConceptAgentOutput)
    assert len(concepts_out.proposals) == 3

    script_out = await runtime.generate_structured(
        stage="scripts",
        inputs={"concept": {"id": "c-1", "hook": "Look here", "offer": "Serum Y"}},
    )
    assert isinstance(script_out, ScriptAgentOutput)
    assert len(script_out.draft.spoken_beats) >= 2
    assert script_out.draft.estimated_duration >= 14

    creators_out = await runtime.generate_structured(
        stage="creator_profiles",
        inputs={"concept_ids": ["c-1", "c-2"]},
    )
    assert isinstance(creators_out, CreatorProfilesAgentOutput)
    assert len(creators_out.creators) == 2
    assert len(creators_out.assignments) == 2


@pytest.mark.asyncio
async def test_mock_language_runtime_unsupported_stage_raises_value_error():
    from orchestrator.language_runtime import _mock_structured_submission

    with pytest.raises(ValueError, match="unsupported terminal mock stage"):
        _mock_structured_submission("unknown_stage", {})


@pytest.mark.asyncio
async def test_non_mock_language_runtime_direct_methods_raise_runtime_error(monkeypatch):
    from orchestrator.language_runtime import LanguageRuntime

    monkeypatch.setenv("AI_GATEWAY_TOKEN", "mock-token")
    runtime = LanguageRuntime.from_provider(
        "vercel_gateway_llm",
        {"pipeline": {"llm_model": "google/gemini-2.5-flash"}},
    )

    with pytest.raises(RuntimeError, match="direct concept generation is only available for the mock runtime"):
        await runtime.generate_concepts(offer="test")

    with pytest.raises(RuntimeError, match="direct script generation is only available for the mock runtime"):
        await runtime.write_script(concept={"id": "c-1"})
