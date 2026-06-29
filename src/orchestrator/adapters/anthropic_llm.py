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

_HOOK_STYLES = ["problem", "curiosity", "bold_claim", "emotional", "social_proof"]
_FORMATS = ["talking_head", "demo", "reaction"]
DEFAULT_VERCEL_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh"
DEFAULT_VERCEL_GATEWAY_MODEL = "anthropic/claude-opus-4.8"

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

    async def generate_concepts(
        self,
        offer: str,
        n: int,
        seed: str,
        bias: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Gera ``n`` conceitos de UGC via Claude com Structured Outputs.

        ``bias`` (opcional) — lista de hook_styles vencedores do ciclo anterior
        (Step 10 → 1). Se não-vazio, o prompt orienta ~60 % dos conceitos para
        esses estilos mantendo spread nos demais.
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

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _CONCEPT_SCHEMA}},
            messages=[{"role": "user", "content": user_prompt}],
        )

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

    async def write_script(
        self,
        concept: dict[str, Any],
        creator_ref: str,
        platform: str,
    ) -> str:
        """Escreve o script de UGC para um conceito, calibrado por plataforma.

        Plataformas como TikTok pedem pacing rápido; outras aceitam ritmo médio.
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

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": user_prompt}],
        )

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
    )
    client = AsyncAnthropic(api_key=token, base_url=base_url)
    return AnthropicLLMAdapter(model=model, client=client)
