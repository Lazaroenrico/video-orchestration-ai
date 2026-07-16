"""GatewayLLMAdapter — adapter LLM gateway-nativo (Vercel AI Gateway, OpenAI-compatible).

Todos os testes usam ``httpx.MockTransport`` — sem rede, sem chave real. Cobrem os dois
ports (LLMPort: concepts/scripts; AgentPort: run_stage_agent/critique) e a fábrica.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx
import pytest

from orchestrator.adapters.gateway_llm import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_LLM_MODEL,
    GatewayLLMAdapter,
    build_gateway_llm_adapter,
)

BASE = "https://gw.test/v1"
TOKEN = "vck_test"


def _chat_response(content: str, *, usage: Optional[dict[str, int]] = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 12, "completion_tokens": 7},
        },
    )


def _adapter_with(handler, **kwargs: Any) -> GatewayLLMAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE)
    return GatewayLLMAdapter(base_url=BASE, token=TOKEN, client=client, **kwargs)


def _concepts_payload(n: int, *, with_ids: bool = True) -> str:
    concepts = []
    for i in range(n):
        c: dict[str, Any] = {
            "offer": "IGNORED",  # o adapter sempre sobrescreve com o argumento
            "hook": f"hook-{i}",
            "angle": "curiosity",
            "hook_style": "curiosity",
            "format": "demo",
        }
        if with_ids:
            c["id"] = f"concept-{i:04d}"
        concepts.append(c)
    return json.dumps({"concepts": concepts})


# --------------------------------------------------------------------------- #
# generate_concepts                                                           #
# --------------------------------------------------------------------------- #


async def test_generate_concepts_returns_n_and_propagates_offer():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == f"{BASE}/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        body = json.loads(request.content)
        assert body["model"] == DEFAULT_GATEWAY_LLM_MODEL
        # Structured Outputs via response_format json_schema
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"]["type"] == "object"
        return _chat_response(_concepts_payload(3))

    adapter = _adapter_with(handler)
    out = await adapter.generate_concepts(offer="serum X", n=3, seed="s")

    assert len(out) == 3
    assert all(c["offer"] == "serum X" for c in out)  # sobrescrito
    assert len(calls) == 1


async def test_generate_concepts_truncates_to_n():
    adapter = _adapter_with(lambda req: _chat_response(_concepts_payload(5)))
    out = await adapter.generate_concepts(offer="o", n=2, seed="s")
    assert len(out) == 2


async def test_generate_concepts_defaults_missing_id():
    adapter = _adapter_with(lambda req: _chat_response(_concepts_payload(1, with_ids=False)))
    out = await adapter.generate_concepts(offer="o", n=1, seed="s")
    assert out[0]["id"] == "concept-0000"


async def test_generate_concepts_bias_included_in_prompt():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_response(_concepts_payload(1))

    adapter = _adapter_with(handler)
    await adapter.generate_concepts(offer="o", n=1, seed="s", bias=["problem", "junk"])
    assert "Bias ~60%" in captured["prompt"]
    assert "problem" in captured["prompt"]
    assert "junk" not in captured["prompt"]  # bias inválido é filtrado


async def test_generate_concepts_no_bias_asks_for_spread():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_response(_concepts_payload(1))

    adapter = _adapter_with(handler)
    await adapter.generate_concepts(offer="o", n=1, seed="s")
    assert "Spread the hook_styles broadly" in captured["prompt"]


async def test_generate_concepts_revision_appended_to_prompt():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_response(_concepts_payload(1))

    adapter = _adapter_with(handler)
    await adapter.generate_concepts(offer="o", n=1, seed="s", revision="punch up the hook")
    assert "REVISION DIRECTIVE" in captured["prompt"]
    assert "punch up the hook" in captured["prompt"]


async def test_generate_concepts_raises_on_missing_choices():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {}})  # sem choices

    adapter = _adapter_with(handler)
    with pytest.raises(RuntimeError, match="missing choices"):
        await adapter.generate_concepts(offer="o", n=1, seed="s")


async def test_generate_concepts_raises_on_empty_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("   ")  # conteúdo em branco

    adapter = _adapter_with(handler)
    with pytest.raises(RuntimeError, match="empty message content"):
        await adapter.generate_concepts(offer="o", n=1, seed="s")


async def test_chat_raises_verbose_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request detail"})

    adapter = _adapter_with(handler)
    with pytest.raises(httpx.HTTPStatusError, match="bad request detail"):
        await adapter.generate_concepts(offer="o", n=1, seed="s")


# --------------------------------------------------------------------------- #
# write_script                                                                #
# --------------------------------------------------------------------------- #


async def test_write_script_returns_text_and_omits_response_format():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _chat_response("HOOK\nBODY\nCTA")

    adapter = _adapter_with(handler)
    out = await adapter.write_script(
        concept={"id": "c-0", "hook": "h"}, creator_ref="creator-1", platform="tiktok"
    )
    assert out == "HOOK\nBODY\nCTA"
    assert "response_format" not in captured["body"]  # texto livre


@pytest.mark.parametrize(
    "platform,marker",
    [
        ("tiktok", "Pacing: FAST"),
        ("reels", "Pacing: MEDIUM-FAST"),
        ("instagram", "Pacing: MEDIUM-FAST"),
        ("youtube", "Pacing: MEDIUM."),
    ],
)
async def test_write_script_pacing_per_platform(platform, marker):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_response("script")

    adapter = _adapter_with(handler)
    await adapter.write_script(concept={"id": "c"}, creator_ref="cr", platform=platform)
    assert marker in captured["prompt"]


async def test_write_script_revision_appended():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["prompt"] = json.loads(request.content)["messages"][0]["content"]
        return _chat_response("script")

    adapter = _adapter_with(handler)
    await adapter.write_script(
        concept={"id": "c"}, creator_ref="cr", platform="tiktok", revision="tighten CTA"
    )
    assert "REVISION DIRECTIVE" in captured["prompt"]
    assert "tighten CTA" in captured["prompt"]


# --------------------------------------------------------------------------- #
# run_stage_agent — loop de tool-calling (Fase 1)                             #
# --------------------------------------------------------------------------- #


def _tool_call_response(name: str, arguments: str = "{}") -> httpx.Response:
    """Resposta OpenAI-compatible com um tool_call (o modelo decide chamar a tool)."""
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call-{name}",
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _queued_handler(responses: list[httpx.Response]):
    """Handler que consome respostas em fila; a última repete (ex.: 'stop')."""
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[i]

    return handler


async def test_run_stage_agent_single_tool_call_then_stop():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **inputs: Any) -> Any:
        calls.append((tool_name, inputs))
        return ["draft-concept"]

    handler = _queued_handler([
        _tool_call_response("generate_concepts"),
        _chat_response("Looks great, done."),  # sem tool_calls → para
    ])
    adapter = _adapter_with(handler)
    result = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o", "n": 1, "seed": "s"},
        target_model="anthropic/claude-opus-4.8",
    )

    assert result == ["draft-concept"]
    assert calls == [("generate_concepts", {})]


async def test_run_stage_agent_iterates_with_revision():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **inputs: Any) -> Any:
        calls.append((tool_name, inputs))
        return f"draft/{inputs.get('revision', '')}"

    handler = _queued_handler([
        _tool_call_response("write_script"),  # draft inicial
        _tool_call_response("write_script", '{"revision": "Strengthen the hook."}'),
        _chat_response("Done."),  # para
    ])
    adapter = _adapter_with(handler)
    result = await adapter.run_stage_agent(
        stage="scripts",
        allowed_tools=("write_script",),
        run_tool=run_tool,
        inputs={"concept": {"id": "c"}, "creator_ref": "cr", "platform": "tiktok"},
    )

    assert len(calls) == 2
    assert calls[1] == ("write_script", {"revision": "Strengthen the hook."})
    assert result == "draft/Strengthen the hook."


async def test_run_stage_agent_respects_step_budget():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **inputs: Any) -> Any:
        calls.append((tool_name, inputs))
        return "draft"

    # O modelo sempre pede outra tool call; sem budget seria infinito.
    handler = _queued_handler([_tool_call_response("generate_concepts")])
    adapter = _adapter_with(handler)
    await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=2,
    )

    assert len(calls) == 2  # cortado no budget


async def test_run_stage_agent_safety_net_when_model_never_calls_tool():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **inputs: Any) -> Any:
        calls.append((tool_name, inputs))
        return ["fallback-draft"]

    # O modelo responde sem nenhum tool_call de cara; a safety-net roda a tool primária.
    adapter = _adapter_with(lambda req: _chat_response("I think we're done."))
    result = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
    )

    assert result == ["fallback-draft"]
    assert calls == [("generate_concepts", {})]


async def test_run_stage_agent_ignores_tool_outside_allowlist():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **inputs: Any) -> Any:
        calls.append((tool_name, inputs))
        return "draft"

    handler = _queued_handler([
        _tool_call_response("delete_everything"),  # fora da allowlist → não roda
        _tool_call_response("generate_concepts"),
        _chat_response("Done."),
    ])
    adapter = _adapter_with(handler)
    await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
    )

    assert ("delete_everything", {}) not in calls
    assert calls == [("generate_concepts", {})]


async def test_run_stage_agent_safety_net_on_malformed_response():
    """Resposta sem ``choices`` → brain trata como 'sem tool call'; safety-net roda a tool."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **inputs: Any) -> Any:
        calls.append((tool_name, inputs))
        return ["draft"]

    adapter = _adapter_with(lambda req: httpx.Response(200, json={}))  # sem choices
    result = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
    )

    assert result == ["draft"]
    assert calls == [("generate_concepts", {})]


def test_gateway_parse_tool_calls_tolerates_bad_arguments():
    from orchestrator.adapters.gateway_llm import _GatewayAgentBrain

    calls = _GatewayAgentBrain._parse_tool_calls(
        {"tool_calls": [{"id": "x", "function": {"name": "generate_concepts", "arguments": "{bad"}}]}
    )
    assert len(calls) == 1
    assert calls[0].name == "generate_concepts"
    assert calls[0].arguments == {}  # JSON inválido → dict vazio


def test_gateway_summarize_result_falls_back_on_unserializable():
    from orchestrator.adapters.gateway_llm import _summarize_result

    circular: dict[str, Any] = {}
    circular["self"] = circular  # referência circular → json.dumps levanta
    out = _summarize_result(circular)
    assert isinstance(out, str) and out


# --------------------------------------------------------------------------- #
# Ramo "client próprio" (self._client is None)                                #
# --------------------------------------------------------------------------- #


async def test_uses_own_client_when_not_injected(monkeypatch):
    import orchestrator.adapters.gateway_llm as gateway_llm

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: _chat_response(_concepts_payload(1)))
    monkeypatch.setattr(
        gateway_llm.httpx, "AsyncClient",
        lambda *a, **k: real_async_client(transport=transport, base_url=BASE),
    )
    adapter = GatewayLLMAdapter(base_url=BASE, token=TOKEN)  # sem client

    out = await adapter.generate_concepts(offer="o", n=1, seed="s")
    assert len(out) == 1


# --------------------------------------------------------------------------- #
# Fábrica                                                                      #
# --------------------------------------------------------------------------- #


def test_build_gateway_llm_adapter_uses_gateway_api_key(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck_env")
    monkeypatch.delenv("AI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("AI_GATEWAY_LLM_MODEL", raising=False)
    adapter = build_gateway_llm_adapter({})
    assert adapter.token == "vck_env"
    assert adapter.base_url == DEFAULT_GATEWAY_BASE_URL
    assert adapter.model == DEFAULT_GATEWAY_LLM_MODEL


def test_build_gateway_llm_adapter_accepts_oidc_token(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc_tok")
    adapter = build_gateway_llm_adapter({})
    assert adapter.token == "oidc_tok"


def test_build_gateway_llm_adapter_requires_auth(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        build_gateway_llm_adapter({})


def test_build_gateway_llm_adapter_model_env_wins(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck")
    monkeypatch.setenv("AI_GATEWAY_LLM_MODEL", "anthropic/claude-sonnet-5")
    adapter = build_gateway_llm_adapter({"llm_model": "ignored"})
    assert adapter.model == "anthropic/claude-sonnet-5"


def test_build_gateway_llm_adapter_model_pipeline_fallback(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck")
    monkeypatch.delenv("AI_GATEWAY_LLM_MODEL", raising=False)
    adapter = build_gateway_llm_adapter({"llm_model": "anthropic/claude-haiku-4-5"})
    assert adapter.model == "anthropic/claude-haiku-4-5"


def test_build_gateway_llm_adapter_base_url_override(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://custom.gw/v1")
    adapter = build_gateway_llm_adapter({})
    assert adapter.base_url == "https://custom.gw/v1"
