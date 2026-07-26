"""GatewayLLMAdapter — adapter LLM **gateway-nativo** (Vercel AI Gateway).

Fala com o gateway via ``httpx`` puro contra ``POST {base_url}/chat/completions``
(OpenAI-compatible) — **sem** o SDK ``anthropic``. Implementa:

- ``LLMPort`` — ``write_persona`` (Step 0), ``generate_concepts`` (Step 1, com JSON
  Schema via ``response_format``) e ``write_script`` (Step 2, texto livre calibrado
  por plataforma).
- ``AgentPort`` — ``run_stage_agent`` (Fase 7 / D31): loop *critique -> refine* bounded,
  com a crítica servida pelo mesmo gateway. O agent só toca o domínio via ``run_tool``
  (fronteira D29) — nunca chama ``generate_concepts``/``write_script`` diretamente.

Transporte espelha ``openai_image.py``: ``httpx.AsyncClient`` injetável (``client=``,
com ``httpx.MockTransport`` nos testes offline), auth ``Authorization: Bearer <token>``,
retry de transporte (``with_transport_retry``) e diagnóstico de erro com corpo do gateway.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

from orchestrator import stream_bus
from orchestrator.adapters._agent_loop import (
    DEFAULT_MAX_STEPS,
    AgentRunResult,
    ToolCall,
    run_agent_loop,
    summarize_tool_result,
)
from orchestrator.adapters._retry import with_transport_retry
from orchestrator.adapters.base import StageToolRunner
from orchestrator.tools.registry import tool_call_schemas
from orchestrator.tracing import add_trace_metadata, record_llm_usage, traced

_HOOK_STYLES = ["problem", "curiosity", "bold_claim", "emotional", "social_proof"]
_FORMATS = ["talking_head", "demo", "reaction"]

DEFAULT_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
DEFAULT_GATEWAY_LLM_MODEL = "anthropic/claude-opus-4.8"
# Opus com thinking pode demorar; timeout generoso evita ReadTimeout intermitente.
DEFAULT_GATEWAY_TIMEOUT = 120.0

# JSON Schema para Structured Outputs de generate_concepts (OpenAI-compatible).
_CONCEPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":         {"type": "string"},
                    "offer":      {"type": "string"},
                    "hook":       {"type": "string"},
                    "angle":      {"type": "string"},
                    "hook_style": {"type": "string", "enum": _HOOK_STYLES},
                    "format":     {"type": "string", "enum": _FORMATS},
                },
                "required": ["id", "offer", "hook", "angle", "hook_style", "format"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["concepts"],
    "additionalProperties": False,
}


def _persona_context(persona: Optional[str]) -> str:
    text = persona.strip() if isinstance(persona, str) else ""
    if not text:
        return ""
    return (
        "PERSONA CONTEXT (must shape the angles, language, and creator POV; do not "
        f"quote it verbatim unless useful):\n{text}\n\n"
    )


def _raise_for_status_verbose(resp: httpx.Response, *, label: str = "") -> None:
    """Raise HTTPStatusError preservando o corpo da resposta (diagnóstico do gateway)."""
    if resp.is_success:
        return
    body = resp.text[:2000]
    prefix = f"{label}: " if label else ""
    message = f"{prefix}{resp.status_code} {resp.reason_phrase} for url '{resp.url}'"
    if body:
        message += f"\nBody: {body}"
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp)


def _sse_payload(line: str) -> Optional[str]:
    """Extrai o payload de uma linha SSE ``data: ...``.

    ``None`` para o que não é dado: linhas vazias (separador de evento), comentários
    de keep-alive (``: ping``) e o sentinela ``[DONE]``.
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    return None if not payload or payload == "[DONE]" else payload


def _openai_usage_to_metric(usage: Any) -> dict[str, int]:
    """Normaliza ``usage`` OpenAI-compatible p/ o shape que ``record_llm_usage`` lê.

    O gateway devolve ``{prompt_tokens, completion_tokens, ...}``; o tracing espera
    ``input_tokens``/``output_tokens`` (contrato do SDK Anthropic). Mapeia os dois.
    """
    data = usage if isinstance(usage, dict) else {}
    return {
        "input_tokens": int(data.get("prompt_tokens") or 0),
        "output_tokens": int(data.get("completion_tokens") or 0),
    }


class GatewayLLMAdapter:
    """Adapter LLM gateway-nativo — implementa LLMPort e AgentPort via httpx.

    Parameters
    ----------
    base_url:
        Base OpenAI-compatible do gateway. Padrão: ``https://ai-gateway.vercel.sh/v1``.
    token:
        Token de auth (``Authorization: Bearer <token>``). Se vazio, lê de
        ``AI_GATEWAY_API_KEY``/``VERCEL_OIDC_TOKEN``.
    model:
        ID do modelo com prefixo de provider (ex.: ``anthropic/claude-opus-4.8``).
    client:
        ``httpx.AsyncClient`` injetado (testes offline via ``MockTransport``). Se
        ``None``, cria um por chamada.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        token: str = "",
        model: str = DEFAULT_GATEWAY_LLM_MODEL,
        timeout: float = DEFAULT_GATEWAY_TIMEOUT,
        client: Optional[httpx.AsyncClient] = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("AI_GATEWAY_API_KEY", "") or os.environ.get(
            "VERCEL_OIDC_TOKEN", ""
        )
        self.model = model
        self.timeout = timeout
        self._client = client
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    # ------------------------------------------------------------------ #
    # Transporte                                                          #
    # ------------------------------------------------------------------ #

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        stage: Optional[str] = None,
    ) -> dict[str, Any]:
        """POST ``{base_url}/chat/completions`` e retorna o JSON decodificado.

        Registra token usage/custo na run atual (via ``record_llm_usage``) e levanta
        com o corpo do gateway em falha (diagnóstico). Retenta blips de transporte/429.
        ``tools`` (function-calling OpenAI-compatible) habilita o loop agentic (Fase 1).

        ``stage`` marca a chamada como *observável*: quando informado **e** houver um
        subscriber no ``stream_bus``, a resposta vem por SSE e cada delta é emitido como
        ``llm_token`` para a UI. Sem ``stage`` (ex.: as rodadas de decisão do agent) a
        chamada nunca streama. Os dois caminhos devolvem **o mesmo shape** — quem chama
        não sabe qual rodou.
        """
        used_model = model or self.model
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": used_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if response_format is not None:
            body["response_format"] = response_format
        if tools is not None:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        streaming = stage is not None and stream_bus.is_streaming()
        if streaming:
            body["stream"] = True
            # Sem isto o gateway omite o usage no SSE e o custo do run seria zero.
            body["stream_options"] = {"include_usage": True}

        url = f"{self.base_url}/chat/completions"

        async def _call() -> dict[str, Any]:
            if streaming:
                return await self._stream_chat(url, headers, body, stage or "")
            if self._client is not None:
                resp = await self._client.post(url, headers=headers, json=body)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, headers=headers, json=body)
            _raise_for_status_verbose(resp, label="gateway_llm")
            return resp.json()

        # Seguro retentar mesmo streamando: ``_is_retryable`` só cobre erros pré-envio
        # (ConnectError/PoolTimeout) e 429 — todos anteriores ao 1º token. Falha no meio
        # do stream é ReadTimeout, que não é retentável; nenhum token é reemitido.
        data: dict[str, Any] = await with_transport_retry(
            _call,
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="gateway_llm.chat",
        )
        record_llm_usage(_openai_usage_to_metric(data.get("usage")), used_model)
        return data

    async def _stream_chat(
        self, url: str, headers: dict[str, str], body: dict[str, Any], stage: str
    ) -> dict[str, Any]:
        """Consome o SSE e remonta a resposta no shape do endpoint não-streaming."""
        if self._client is not None:
            return await self._consume_sse(self._client, url, headers, body, stage)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await self._consume_sse(client, url, headers, body, stage)

    @staticmethod
    async def _consume_sse(
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        stage: str,
    ) -> dict[str, Any]:
        """Emite cada delta como ``llm_token`` e devolve a resposta remontada.

        Equivale ao ``get_final_message()`` do SDK Anthropic: streaming muda **como** o
        texto chega, não **o que** o modelo produz — por isso o retorno imita
        ``choices[0].message.content`` + ``usage``, e ``_message_text``/
        ``record_llm_usage`` seguem inalterados.
        """
        parts: list[str] = []
        usage: Optional[dict[str, Any]] = None
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if not resp.is_success:
                # O corpo de erro não foi lido ainda (resposta streamada); sem isto o
                # diagnóstico do gateway viria vazio.
                await resp.aread()
                _raise_for_status_verbose(resp, label="gateway_llm")
            # Só depois do status OK: um retry pré-envio não deve emitir llm_start.
            stream_bus.emit_token({"type": "llm_start", "stage": stage})
            try:
                async for line in resp.aiter_lines():
                    payload = _sse_payload(line)
                    if payload is None:
                        continue
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue  # keep-alive/linha malformada não derruba o stream
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        token = (choice.get("delta") or {}).get("content")
                        if token:
                            parts.append(token)
                            stream_bus.emit_token(
                                {"type": "llm_token", "stage": stage, "token": token}
                            )
            finally:
                # Em finally para a UI não ficar com o indicador preso se o stream cair.
                stream_bus.emit_token({"type": "llm_end", "stage": stage})
        return {"choices": [{"message": {"content": "".join(parts)}}], "usage": usage}

    @staticmethod
    def _message_text(data: dict[str, Any]) -> str:
        """Extrai ``choices[0].message.content`` como texto (erro claro se ausente)."""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"gateway response missing choices[0].message.content: {data!r}"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"gateway response has empty message content: {data!r}")
        return content

    # ------------------------------------------------------------------ #
    # Step 0 — Persona                                                    #
    # ------------------------------------------------------------------ #

    @traced("adapter.gateway.write_persona", run_type="llm", step=0, provider="vercel_gateway")
    async def write_persona(
        self,
        offer: str,
        brief: Optional[str] = None,
        revision: Optional[str] = None,
    ) -> str:
        """Escreve a persona batch-level usada por concepts, scripts e creator."""
        brief_text = brief.strip() if isinstance(brief, str) else ""
        user_prompt = (
            "Write one concise batch-level UGC creator persona for this offer.\n\n"
            f"OFFER: {offer}\n\n"
            "The persona should define audience, creator POV, tone, trust posture, "
            "language style, and claim boundaries. Keep it practical for downstream "
            "concept, script, image, and voice prompts."
        )
        if brief_text:
            user_prompt += f"\n\nBRIEF: {brief_text}"
        if revision:
            user_prompt += (
                f"\n\nREVISION DIRECTIVE (address this in the rewritten persona): {revision}"
            )

        data = await self._chat(
            [{"role": "user", "content": user_prompt}],
            max_tokens=2000,
            stage="persona",
        )
        return self._message_text(data)

    # ------------------------------------------------------------------ #
    # Step 1 — Conceitos                                                  #
    # ------------------------------------------------------------------ #

    @traced("adapter.gateway.generate_concepts", run_type="llm", step=1, provider="vercel_gateway")
    async def generate_concepts(
        self,
        offer: str,
        n: int,
        seed: str,
        bias: Optional[list[str]] = None,
        revision: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Gera ``n`` conceitos de UGC via gateway com Structured Outputs (JSON Schema).

        ``bias`` — hook_styles vencedores do ciclo anterior (Step 10 -> 1); orienta ~60%.
        ``revision`` (Fase 7) — diretiva de refino do agent anexada ao prompt.
        """
        valid_bias = [b for b in (bias or []) if b in _HOOK_STYLES]
        if valid_bias:
            bias_instruction = (
                f"Bias ~60% of the concepts toward these hook_styles (from the previous "
                f"cycle's winners): {valid_bias}. Spread the remaining ~40% across other "
                f"styles to maintain diversity."
            )
        else:
            bias_instruction = (
                f"Spread the hook_styles broadly across all 5 styles: {_HOOK_STYLES}."
            )

        user_prompt = _persona_context(persona) + (
            f"Generate exactly {n} UGC ad concepts for the following offer:\n\n"
            f"OFFER: {offer}\n\n"
            f"SEED (use for determinism): {seed}\n\n"
            f"{bias_instruction}\n\n"
            "For each concept provide:\n"
            "- id: a short unique slug like 'concept-0001'\n"
            "- offer: the exact offer string above\n"
            "- hook: a punchy opening hook line\n"
            "- angle: same value as hook_style (the creative angle name)\n"
            f"- hook_style: one of {_HOOK_STYLES}\n"
            f"- format: one of {_FORMATS}\n\n"
            f"Return exactly {n} items in the 'concepts' array."
        )
        if revision:
            user_prompt += (
                f"\n\nREVISION DIRECTIVE (address this in the regenerated concepts): {revision}"
            )

        data = await self._chat(
            [{"role": "user", "content": user_prompt}],
            max_tokens=16000,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "concepts",
                    "strict": True,
                    "schema": _CONCEPT_SCHEMA,
                },
            },
            stage="concepts",
        )
        parsed: dict[str, Any] = json.loads(self._message_text(data))
        raw_concepts: list[dict[str, Any]] = parsed["concepts"]

        concepts: list[dict[str, Any]] = []
        for i, c in enumerate(raw_concepts[:n]):
            c.setdefault("id", f"concept-{i:04d}")
            c["offer"] = offer  # sempre propagado do argumento recebido
            concepts.append(c)
        return concepts

    # ------------------------------------------------------------------ #
    # Step 2 — Scripts                                                    #
    # ------------------------------------------------------------------ #

    @traced("adapter.gateway.write_script", run_type="llm", step=2, provider="vercel_gateway")
    async def write_script(
        self,
        concept: dict[str, Any],
        creator_ref: str,
        platform: str,
        revision: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> str:
        """Escreve o script de UGC para um conceito, calibrado por plataforma.

        ``revision`` (Fase 7) — diretiva de refino do agent anexada ao prompt.
        """
        platform_lower = platform.lower()
        if platform_lower == "tiktok":
            pacing_note = (
                "Pacing: FAST. Hook must land in the first 2 seconds. "
                "Keep sentences punchy and short. Max 45 seconds total runtime."
            )
        elif platform_lower in ("instagram", "reels"):
            pacing_note = (
                "Pacing: MEDIUM-FAST. Hook within 3 seconds. "
                "Keep it energetic but slightly more room for story."
            )
        else:
            pacing_note = (
                "Pacing: MEDIUM. You have more room for context and story. "
                "Hook within 5 seconds."
            )

        user_prompt = _persona_context(persona) + (
            f"Write a UGC ad script for the following concept.\n\n"
            f"Platform: {platform}\n"
            f"{pacing_note}\n\n"
            f"Creator reference: {creator_ref}\n\n"
            f"Concept details:\n"
            f"  Offer: {concept.get('offer', '')}\n"
            f"  Hook style: {concept.get('hook_style', '')}\n"
            f"  Hook line: {concept.get('hook', '')}\n"
            f"  Angle: {concept.get('angle', '')}\n"
            f"  Format: {concept.get('format', '')}\n\n"
            "Structure the script with clearly labeled sections: HOOK, BODY, CTA."
        )
        if revision:
            user_prompt += (
                f"\n\nREVISION DIRECTIVE (address this in the rewritten script): {revision}"
            )

        data = await self._chat(
            [{"role": "user", "content": user_prompt}],
            max_tokens=2000,
            # Label com o id do conceito: a UI separa um script por card (paridade
            # com o AnthropicLLMAdapter).
            stage=f"script:{concept.get('id', '')}",
        )
        return self._message_text(data)

    # ------------------------------------------------------------------ #
    # Fase 1 — execução agentic (concepts/scripts): loop de tool-calling   #
    # ------------------------------------------------------------------ #

    async def run_stage_agent(
        self,
        *,
        stage: str,
        allowed_tools: tuple[str, ...],
        run_tool: StageToolRunner,
        inputs: dict[str, Any],
        target_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tool_calls: Optional[int] = None,
    ) -> AgentRunResult:
        """Loop de tool-calling real, com o modelo servido pelo AI gateway.

        O modelo recebe os schemas das tools permitidas e decide quais chamar (e com que
        ``revision``), iterando até convergir ou estourar ``max_steps``. O agent só toca
        o domínio via ``run_tool`` (fronteira D29) — a geração real (concepts/scripts/
        clips) acontece dentro da typed tool, nunca por chamada direta.
        """
        brain = _GatewayAgentBrain(
            self,
            model=target_model or self.model,
            system_prompt=system_prompt,
        )
        run = await run_agent_loop(
            brain,
            stage=stage,
            allowed_tools=allowed_tools,
            run_tool=run_tool,
            inputs=inputs,
            max_steps=max_steps,
            tool_schemas=tool_call_schemas(allowed_tools),
            max_tool_calls=max_tool_calls,
        )
        add_trace_metadata(
            agent_backend="vercel_gateway",
            stage=stage,
            allowed_tools=list(allowed_tools),
            target_model=target_model,
            agent_steps=run.executed,
        )
        return run


# --------------------------------------------------------------------------- #
# Brain do loop de tool-calling (OpenAI-compatible)                           #
# --------------------------------------------------------------------------- #

_AGENT_SYSTEM_PROMPT = (
    "You are an agent driving the '{stage}' stage of a UGC ad pipeline. "
    "Call the provided tool to produce the draft. Then review the tool result: if it can "
    "be materially improved, call the tool again passing a concise one-line 'revision' "
    "directive. When the result is strong, stop and reply without any further tool call. "
    "You may only set an optional 'revision'; the other inputs are fixed server-side."
)


def _agent_system_prompt(stage: str, configured_prompt: Optional[str]) -> str:
    prompt = configured_prompt.strip() if isinstance(configured_prompt, str) else ""
    return prompt or _AGENT_SYSTEM_PROMPT.format(stage=stage)


# Compartilhado com o adapter Anthropic: elide data URIs (mídia base64) e trunca, para o
# resultado de uma tool de vídeo não queimar o contexto do modelo (D33).
_summarize_result = summarize_tool_result


class _GatewayAgentBrain:
    """Ponte OpenAI-compatible (function-calling) entre o loop e o AI gateway."""

    def __init__(
        self,
        adapter: "GatewayLLMAdapter",
        *,
        model: str,
        system_prompt: Optional[str] = None,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._system_prompt = system_prompt

    @staticmethod
    def _openai_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["parameters"],
                },
            }
            for s in tool_schemas
        ]

    def initial_messages(
        self, stage: str, inputs: dict[str, Any], tool_schemas: list[dict[str, Any]]
    ) -> list[Any]:
        return [
            {"role": "system", "content": _agent_system_prompt(stage, self._system_prompt)},
            {
                "role": "user",
                "content": (
                    f"Stage inputs (fixed): {json.dumps(inputs, default=str)}\n"
                    "Begin by calling the tool to produce the initial draft."
                ),
            },
        ]

    async def complete(
        self, messages: list[Any], tool_schemas: list[dict[str, Any]]
    ) -> tuple[Any, list[ToolCall]]:
        data = await self._adapter._chat(
            messages,
            max_tokens=1500,
            model=self._model,
            tools=self._openai_tools(tool_schemas),
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return {"role": "assistant", "content": ""}, []
        return message, self._parse_tool_calls(message)

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
        raw = message.get("tool_calls") or []
        calls: list[ToolCall] = []
        for tc in raw:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except (TypeError, ValueError):
                args = {}
            calls.append(
                ToolCall(
                    id=str(tc.get("id", "")),
                    name=str(fn.get("name", "")),
                    arguments=args if isinstance(args, dict) else {},
                )
            )
        return calls

    def tool_result_message(self, call: ToolCall, result: Any) -> Any:
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": _summarize_result(result),
        }


# --------------------------------------------------------------------------- #
# Fábrica injetável (usada pelo registry.py)                                  #
# --------------------------------------------------------------------------- #

def build_gateway_llm_adapter(pipeline: dict[str, Any]) -> GatewayLLMAdapter:
    """Cria um GatewayLLMAdapter apontado para o Vercel AI Gateway (gateway-nativo).

    Token: ``AI_GATEWAY_API_KEY`` (fallback ``VERCEL_OIDC_TOKEN``). Base e model
    sobrescrevíveis por env (``AI_GATEWAY_BASE_URL``, ``AI_GATEWAY_LLM_MODEL``) ou
    ``pipeline['llm_model']``.
    """
    token = os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("VERCEL_OIDC_TOKEN")
    if not token:
        raise RuntimeError(
            "AI_GATEWAY_API_KEY or VERCEL_OIDC_TOKEN is required for vercel_gateway_llm"
        )
    base_url = os.environ.get("AI_GATEWAY_BASE_URL", DEFAULT_GATEWAY_BASE_URL)
    model = (
        os.environ.get("AI_GATEWAY_LLM_MODEL")
        or pipeline.get("llm_model")
        or DEFAULT_GATEWAY_LLM_MODEL
    )
    return GatewayLLMAdapter(base_url=base_url, token=token, model=model)
