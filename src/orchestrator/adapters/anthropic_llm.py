"""AnthropicLLMAdapter — adapter real do Claude para LLMPort.

Implementa os dois métodos do LLMPort usando o SDK oficial ``anthropic``:
- ``generate_concepts`` — usa Structured Outputs via ``output_config`` para
  garantir JSON com o esquema exato que o grafo espera.
- ``write_script`` — chamada de mensagem padrão com texto livre, calibrada
  por plataforma.

Notas de API (claude-opus-4-8):
- ``thinking={"type": "adaptive"}`` é suportado e recomendado.
- ``temperature``, ``top_p``, ``top_k`` e ``budget_tokens`` NÃO devem ser
  passados ao Opus 4.8 (retornam 400).
- ``output_config`` força JSON Schema; é passado junto com ``thinking`` sem
  conflito na versão 0.112.0+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from anthropic import AsyncAnthropic

from orchestrator import stream_bus
from orchestrator.adapters._agent_loop import DEFAULT_MAX_STEPS, ToolCall, run_agent_loop
from orchestrator.adapters.base import StageToolRunner
from orchestrator.tools.registry import tool_call_schemas
from orchestrator.tracing import add_trace_metadata, record_llm_usage, traced

_HOOK_STYLES = ["problem", "curiosity", "bold_claim", "emotional", "social_proof"]
_FORMATS = ["talking_head", "demo", "reaction"]
DEFAULT_VERCEL_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh"
DEFAULT_VERCEL_GATEWAY_MODEL = "anthropic/claude-opus-4.8"
# Opus 4.8 com thinking adaptive pode demorar; o connect timeout padrão de 5s do
# SDK Anthropic causa APITimeoutError intermitente atrás do gateway. Timeout
# generoso + retries tornam o caminho live robusto a blips de conexão.
DEFAULT_VERCEL_GATEWAY_TIMEOUT = 120.0
DEFAULT_VERCEL_GATEWAY_MAX_RETRIES = 4

# JSON Schema para Structured Outputs de generate_concepts
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


class AnthropicLLMAdapter:
    """Adapter real do Claude — implementa LLMPort.

    Parameters
    ----------
    model:
        ID do modelo. Padrão: ``"claude-opus-4-8"``.
        client:
        Instância de ``AsyncAnthropic`` injetável (para testes offline).
        Se ``None``, cria ``AsyncAnthropic()`` que lê ``ANTHROPIC_API_KEY``
        do ambiente.
    """

    def __init__(
        self,
        model: str = "claude-opus-4-8",
        client: Optional[AsyncAnthropic] = None,
    ) -> None:
        self.model = model
        self._client: AsyncAnthropic = client if client is not None else AsyncAnthropic()

    # ------------------------------------------------------------------ #
    # Step 1 — Conceitos                                                   #
    # ------------------------------------------------------------------ #

    @traced("adapter.anthropic.generate_concepts", run_type="llm", step=1, provider="anthropic")
    async def generate_concepts(
        self,
        offer: str,
        n: int,
        seed: str,
        bias: Optional[list[str]] = None,
        revision: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Gera ``n`` conceitos de UGC via Claude com Structured Outputs.

        ``bias`` (opcional) — lista de hook_styles vencedores do ciclo anterior
        (Step 10 → 1). Se não-vazio, o prompt orienta ~60 % dos conceitos para
        esses estilos mantendo spread nos demais.

        ``revision`` (opcional, Fase 7) — diretiva de refino do agent; quando setada,
        é anexada ao prompt para regenerar o batch atendendo à crítica.
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
                "Spread the hook_styles broadly across all 5 styles: "
                f"{_HOOK_STYLES}."
            )

        user_prompt = (
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

        api_kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _CONCEPT_SCHEMA}},
            messages=[{"role": "user", "content": user_prompt}],
        )

        if stream_bus.is_streaming():
            stream_bus.emit_token({"type": "llm_start", "stage": "concepts"})
            async with self._client.messages.stream(**api_kwargs) as s:
                async for text in s.text_stream:
                    stream_bus.emit_token({"type": "llm_token", "stage": "concepts", "token": text})
                response = await s.get_final_message()
            stream_bus.emit_token({"type": "llm_end", "stage": "concepts"})
        else:
            response = await self._client.messages.create(**api_kwargs)

        record_llm_usage(response.usage, self.model)

        if response.stop_reason == "refusal":
            raise RuntimeError(
                f"Claude refused to generate concepts for offer={offer!r}. "
                f"stop_reason='refusal'"
            )

        # Extrai o primeiro bloco de texto
        text_block = next(
            (blk for blk in response.content if blk.type == "text"), None
        )
        if text_block is None:
            raise RuntimeError(
                "Claude response contained no text block. "
                f"Content types: {[b.type for b in response.content]}"
            )

        data: dict[str, Any] = json.loads(text_block.text)
        raw_concepts: list[dict[str, Any]] = data["concepts"]

        # Garante campos obrigatórios e trunca para n
        concepts: list[dict[str, Any]] = []
        for i, c in enumerate(raw_concepts[:n]):
            c.setdefault("id", f"concept-{i:04d}")
            c["offer"] = offer  # sempre propagado do argumento recebido
            concepts.append(c)

        return concepts

    # ------------------------------------------------------------------ #
    # Step 2 — Scripts                                                     #
    # ------------------------------------------------------------------ #

    @traced("adapter.anthropic.write_script", run_type="llm", step=2, provider="anthropic")
    async def write_script(
        self,
        concept: dict[str, Any],
        creator_ref: str,
        platform: str,
        revision: Optional[str] = None,
    ) -> str:
        """Escreve o script de UGC para um conceito, calibrado por plataforma.

        Plataformas como TikTok pedem pacing rápido; outras aceitam ritmo médio.

        ``revision`` (opcional, Fase 7) — diretiva de refino do agent anexada ao prompt.
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

        user_prompt = (
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

        api_kwargs = dict(
            model=self.model,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": user_prompt}],
        )
        stage_label = f"script:{concept.get('id', '')}"

        if stream_bus.is_streaming():
            stream_bus.emit_token({"type": "llm_start", "stage": stage_label})
            async with self._client.messages.stream(**api_kwargs) as s:
                async for text in s.text_stream:
                    stream_bus.emit_token({"type": "llm_token", "stage": stage_label, "token": text})
                response = await s.get_final_message()
            stream_bus.emit_token({"type": "llm_end", "stage": stage_label})
        else:
            response = await self._client.messages.create(**api_kwargs)

        record_llm_usage(response.usage, self.model)

        if response.stop_reason == "refusal":
            raise RuntimeError(
                f"Claude refused to write script for concept id={concept.get('id')!r}. "
                f"stop_reason='refusal'"
            )

        text_block = next(
            (blk for blk in response.content if blk.type == "text"), None
        )
        if text_block is None:
            raise RuntimeError(
                "Claude response contained no text block for write_script. "
                f"Content types: {[b.type for b in response.content]}"
            )

        return text_block.text

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
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> Any:
        """Loop de tool-calling real, com o modelo Claude via SDK.

        O modelo recebe os schemas das tools permitidas (``input_schema``) e decide quais
        chamar (e com que ``revision``), iterando até convergir ou estourar ``max_steps``.
        O agent só toca o domínio via ``run_tool`` (fronteira D29) — a geração real
        (concepts/scripts) acontece dentro da typed tool.
        """
        brain = _AnthropicAgentBrain(self, model=target_model or self.model)
        result, executed = await run_agent_loop(
            brain,
            stage=stage,
            allowed_tools=allowed_tools,
            run_tool=run_tool,
            inputs=inputs,
            max_steps=max_steps,
            tool_schemas=tool_call_schemas(allowed_tools),
        )
        add_trace_metadata(
            agent_backend="anthropic_gateway",
            stage=stage,
            allowed_tools=list(allowed_tools),
            target_model=target_model,
            agent_steps=executed,
        )
        return result


# --------------------------------------------------------------------------- #
# Brain do loop de tool-calling (SDK Anthropic)                               #
# --------------------------------------------------------------------------- #

_AGENT_SYSTEM_PROMPT = (
    "You are an agent driving the '{stage}' stage of a UGC ad pipeline. "
    "Call the provided tool to produce the draft. Then review the tool result: if it can "
    "be materially improved, call the tool again passing a concise one-line 'revision' "
    "directive. When the result is strong, stop and reply without any further tool call. "
    "You may only set an optional 'revision'; the other inputs are fixed server-side."
)


def _summarize_result(result: Any) -> str:
    """Serializa o resultado de uma tool para devolver ao modelo (truncado)."""
    try:
        return json.dumps(result, default=str)[:4000]
    except (TypeError, ValueError):
        return repr(result)[:4000]


class _AnthropicAgentBrain:
    """Ponte entre o loop de tool-calling e o SDK Anthropic (blocos ``tool_use``)."""

    def __init__(self, adapter: "AnthropicLLMAdapter", *, model: str) -> None:
        self._adapter = adapter
        self._model = model
        self._system = ""

    @staticmethod
    def _anthropic_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": s["name"],
                "description": s["description"],
                "input_schema": s["parameters"],
            }
            for s in tool_schemas
        ]

    def initial_messages(
        self, stage: str, inputs: dict[str, Any], tool_schemas: list[dict[str, Any]]
    ) -> list[Any]:
        self._system = _AGENT_SYSTEM_PROMPT.format(stage=stage)
        return [
            {
                "role": "user",
                "content": (
                    f"Stage inputs (fixed): {json.dumps(inputs, default=str)}\n"
                    "Begin by calling the tool to produce the initial draft."
                ),
            }
        ]

    async def complete(
        self, messages: list[Any], tool_schemas: list[dict[str, Any]]
    ) -> tuple[Any, list[ToolCall]]:
        response = await self._adapter._client.messages.create(
            model=self._model,
            max_tokens=1500,
            system=self._system,
            messages=messages,
            tools=self._anthropic_tools(tool_schemas),
        )
        record_llm_usage(response.usage, self._model)
        assistant_message = {"role": "assistant", "content": response.content}
        if response.stop_reason == "refusal":
            return assistant_message, []
        calls = [
            ToolCall(id=str(blk.id), name=str(blk.name), arguments=dict(blk.input or {}))
            for blk in response.content
            if getattr(blk, "type", None) == "tool_use"
        ]
        return assistant_message, calls

    def tool_result_message(self, call: ToolCall, result: Any) -> Any:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": _summarize_result(result),
                }
            ],
        }


# --------------------------------------------------------------------------- #
# Fábrica injetável (usada pelo registry.py)                                  #
# --------------------------------------------------------------------------- #

def build_anthropic_llm_adapter(pipeline: dict[str, Any]) -> AnthropicLLMAdapter:
    """Cria um AnthropicLLMAdapter a partir do bloco de pipeline do YAML.

    Lê ``pipeline.get("llm_model")`` se fornecido; caso contrário usa o padrão.
    A chave de API vem de ``ANTHROPIC_API_KEY`` no ambiente.
    """
    model = pipeline.get("llm_model", "claude-opus-4-8")
    return AnthropicLLMAdapter(model=model)


def build_vercel_gateway_llm_adapter(pipeline: dict[str, Any]) -> AnthropicLLMAdapter:
    """Cria um AnthropicLLMAdapter apontado para o Vercel AI Gateway."""
    token = os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("VERCEL_OIDC_TOKEN")
    if not token:
        raise RuntimeError(
            "AI_GATEWAY_API_KEY or VERCEL_OIDC_TOKEN is required for vercel_gateway_llm"
        )

    model = (
        os.environ.get("AI_GATEWAY_LLM_MODEL")
        or pipeline.get("llm_model")
        or DEFAULT_VERCEL_GATEWAY_MODEL
    )
    base_url = (
        os.environ.get("AI_GATEWAY_BASE_URL")
        or DEFAULT_VERCEL_GATEWAY_BASE_URL
    ).rstrip("/")
    # O Anthropic SDK acrescenta /v1 automaticamente — remover se já incluído
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    client = AsyncAnthropic(
        api_key=token,
        base_url=base_url,
        timeout=DEFAULT_VERCEL_GATEWAY_TIMEOUT,
        max_retries=DEFAULT_VERCEL_GATEWAY_MAX_RETRIES,
    )
    return AnthropicLLMAdapter(model=model, client=client)
