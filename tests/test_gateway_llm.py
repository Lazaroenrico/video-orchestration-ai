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
    run = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o", "n": 1, "seed": "s"},
        target_model="anthropic/claude-opus-4.8",
    )

    assert run.result == ["draft-concept"]
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
    run = await adapter.run_stage_agent(
        stage="scripts",
        allowed_tools=("write_script",),
        run_tool=run_tool,
        inputs={"concept": {"id": "c"}, "creator_ref": "cr", "platform": "tiktok"},
    )

    assert len(calls) == 2
    assert calls[1] == ("write_script", {"revision": "Strengthen the hook."})
    assert run.result == "draft/Strengthen the hook."


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
    run = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
    )

    assert run.result == ["fallback-draft"]
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
    run = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
    )

    assert run.result == ["draft"]
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


async def test_uses_own_client_when_not_injected_while_streaming(monkeypatch, stream_events):
    """Espelho do teste acima no ramo de streaming: produção não injeta client."""
    import orchestrator.adapters.gateway_llm as gateway_llm

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: _sse([_delta(_concepts_payload(1))]))
    monkeypatch.setattr(
        gateway_llm.httpx, "AsyncClient",
        lambda *a, **k: real_async_client(transport=transport, base_url=BASE),
    )
    adapter = GatewayLLMAdapter(base_url=BASE, token=TOKEN)  # sem client

    out = await adapter.generate_concepts(offer="o", n=1, seed="s")

    assert len(out) == 1
    assert [e["type"] for e in stream_events] == ["llm_start", "llm_token", "llm_end"]


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


# --------------------------------------------------------------------------- #
# Streaming SSE (Fase 3) — paridade com o AnthropicLLMAdapter                  #
# --------------------------------------------------------------------------- #


def _sse(chunks: list[dict[str, Any]], *, done: bool = True) -> httpx.Response:
    """Resposta SSE OpenAI-compatible (``data: {...}`` por linha)."""
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _delta(text: str) -> dict[str, Any]:
    return {"choices": [{"delta": {"content": text}}]}


@pytest.fixture
def stream_events():
    """Ativa o stream_bus e coleta os eventos emitidos."""
    import orchestrator.stream_bus as stream_bus

    events: list[dict[str, Any]] = []
    stream_bus.set_token_callback(events.append)
    try:
        yield events
    finally:
        stream_bus.clear_token_callback()


async def test_generate_concepts_streams_tokens_when_streaming(stream_events):
    """Com o bus ativo, os deltas SSE viram llm_token e o resultado final é idêntico."""
    payload = _concepts_payload(1)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        # quebra o JSON em 3 pedaços, como o modelo faria
        thirds = [payload[:5], payload[5:20], payload[20:]]
        return _sse(
            [_delta(t) for t in thirds]
            + [{"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}]
        )

    adapter = _adapter_with(handler)
    concepts = await adapter.generate_concepts(offer="serum", n=1, seed="s")

    # O output de domínio não muda por causa do streaming.
    assert len(concepts) == 1
    assert concepts[0]["offer"] == "serum"
    # O pedido saiu como stream, pedindo usage no chunk final.
    assert seen[0]["stream"] is True
    assert seen[0]["stream_options"] == {"include_usage": True}
    # Eventos: start, N tokens, end — todos com o stage.
    assert stream_events[0] == {"type": "llm_start", "stage": "concepts"}
    assert stream_events[-1] == {"type": "llm_end", "stage": "concepts"}
    tokens = [e["token"] for e in stream_events if e["type"] == "llm_token"]
    assert "".join(tokens) == payload
    assert all(e["stage"] == "concepts" for e in stream_events)


async def test_write_script_streams_tokens_with_the_concept_stage_label(stream_events):
    """O label do stage carrega o id do conceito — a UI separa um script por card."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse([_delta("HOOK: "), _delta("buy now")])

    adapter = _adapter_with(handler)
    out = await adapter.write_script(
        concept={"id": "concept-0007", "offer": "o"}, creator_ref="c", platform="tiktok"
    )

    assert out == "HOOK: buy now"
    assert {e["stage"] for e in stream_events} == {"script:concept-0007"}


async def test_streaming_records_usage_from_the_final_chunk(stream_events, monkeypatch):
    """O usage vem no último chunk SSE; sem isso o custo do run seria subnotificado."""
    from orchestrator.adapters import gateway_llm

    recorded: list[Any] = []
    monkeypatch.setattr(gateway_llm, "record_llm_usage", lambda u, m: recorded.append((u, m)))

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(
            [_delta(_concepts_payload(1))]
            + [{"choices": [], "usage": {"prompt_tokens": 31, "completion_tokens": 12}}]
        )

    await _adapter_with(handler).generate_concepts(offer="serum", n=1, seed="s")

    assert recorded == [({"input_tokens": 31, "output_tokens": 12}, DEFAULT_GATEWAY_LLM_MODEL)]


async def test_no_streaming_keeps_the_plain_post():
    """Sem bus ativo (CLI/testes), o corpo não pede stream — comportamento intacto."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _chat_response(_concepts_payload(1))

    await _adapter_with(handler).generate_concepts(offer="serum", n=1, seed="s")

    assert "stream" not in seen[0]
    assert "stream_options" not in seen[0]


async def test_agent_brain_never_streams(stream_events):
    """O loop agentic não streama: quem streama é a chamada de domínio (paridade D31).

    Evita ter de remontar ``tool_calls`` fragmentados do SSE, e é o que a UI espera —
    o usuário vê o conceito sendo escrito, não a deliberação do agent.
    """
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async def run_tool(tool_name: str, **kwargs: Any) -> Any:
        return ["draft"]

    adapter = _adapter_with(handler)
    run = await adapter.run_stage_agent(
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
    )

    assert run.result == ["draft"]
    assert all("stream" not in body for body in seen)
    assert stream_events == []


async def test_streaming_http_error_still_raises_with_the_gateway_body(stream_events):
    """Erro no stream preserva o corpo do gateway (diagnóstico) e não emite tokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"model not allowed")

    adapter = _adapter_with(handler, max_retries=0, backoff_base=0)
    with pytest.raises(httpx.HTTPStatusError, match="model not allowed"):
        await adapter.generate_concepts(offer="serum", n=1, seed="s")

    assert not [e for e in stream_events if e["type"] == "llm_token"]


async def test_streaming_ignores_malformed_sse_lines(stream_events):
    """Keep-alives, comentários e JSON quebrado não derrubam o stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            ": keep-alive\n\n"
            "data: not-json\n\n"
            f"data: {json.dumps(_delta(_concepts_payload(1)))}\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode())

    concepts = await _adapter_with(handler).generate_concepts(offer="serum", n=1, seed="s")

    assert len(concepts) == 1
