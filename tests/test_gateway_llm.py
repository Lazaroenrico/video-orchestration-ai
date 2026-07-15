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
# run_stage_agent / _agent_critique                                           #
# --------------------------------------------------------------------------- #


async def test_run_stage_agent_approves_does_single_tool_call():
    tool_calls: list[dict[str, Any]] = []

    async def run_tool(**inputs: Any) -> Any:
        tool_calls.append(inputs)
        return ["draft-concept"]

    adapter = _adapter_with(lambda req: _chat_response("APPROVE"))
    result = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o", "n": 1, "seed": "s"},
        target_model="anthropic/claude-opus-4.8",
    )

    assert result == ["draft-concept"]
    assert len(tool_calls) == 1  # sem refino
    assert "revision" not in tool_calls[0]


async def test_run_stage_agent_refines_does_two_tool_calls():
    tool_calls: list[dict[str, Any]] = []

    async def run_tool(**inputs: Any) -> Any:
        tool_calls.append(inputs)
        return f"draft/{inputs.get('revision', '')}"

    adapter = _adapter_with(lambda req: _chat_response("Strengthen the hook."))
    result = await adapter.run_stage_agent(
        stage="scripts",
        allowed_tools=("write_script",),
        run_tool=run_tool,
        inputs={"concept": {"id": "c"}, "creator_ref": "cr", "platform": "tiktok"},
    )

    assert len(tool_calls) == 2
    assert tool_calls[1]["revision"] == "Strengthen the hook."
    assert result == "draft/Strengthen the hook."


async def test_agent_critique_approve_returns_none():
    adapter = _adapter_with(lambda req: _chat_response("APPROVE"))
    assert await adapter._agent_critique("concepts", ["d"], model="m") is None


async def test_agent_critique_empty_returns_none():
    adapter = _adapter_with(lambda req: _chat_response("   "))
    # conteúdo em branco levanta em _message_text → capturado → aprova (None)
    assert await adapter._agent_critique("concepts", ["d"], model="m") is None


async def test_agent_critique_http_failure_returns_none():
    adapter = _adapter_with(lambda req: httpx.Response(500, json={"error": "boom"}))
    assert await adapter._agent_critique("concepts", ["d"], model="m") is None


async def test_agent_critique_returns_directive():
    adapter = _adapter_with(lambda req: _chat_response("Tighten the CTA line."))
    assert await adapter._agent_critique("scripts", "s", model="m") == "Tighten the CTA line."


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
