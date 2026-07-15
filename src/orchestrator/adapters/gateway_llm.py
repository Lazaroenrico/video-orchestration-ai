"""GatewayLLMAdapter — adapter LLM **gateway-nativo** (Vercel AI Gateway).

Fala com o gateway via ``httpx`` puro contra ``POST {base_url}/chat/completions``
(OpenAI-compatible) — **sem** o SDK ``anthropic``. Implementa:

- ``LLMPort`` — ``generate_concepts`` (Step 1, com JSON Schema via ``response_format``)
  e ``write_script`` (Step 2, texto livre calibrado por plataforma).
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

from orchestrator.adapters._retry import with_transport_retry
from orchestrator.adapters.base import StageToolRunner
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
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        model: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """POST ``{base_url}/chat/completions`` e retorna o JSON decodificado.

        Registra token usage/custo na run atual (via ``record_llm_usage``) e levanta
        com o corpo do gateway em falha (diagnóstico). Retenta blips de transporte/429.
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

        async def _call() -> dict[str, Any]:
            if self._client is not None:
                resp = await self._client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=body
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=body
                    )
            _raise_for_status_verbose(resp, label="gateway_llm")
            return resp.json()

        data: dict[str, Any] = await with_transport_retry(
            _call,
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="gateway_llm.chat",
        )
        record_llm_usage(_openai_usage_to_metric(data.get("usage")), used_model)
        return data

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

        data = await self._chat(
            [{"role": "user", "content": user_prompt}], max_tokens=2000
        )
        return self._message_text(data)

    # ------------------------------------------------------------------ #
    # Fase 7 — execução agentic (concepts/scripts) pelo AI gateway         #
    # ------------------------------------------------------------------ #

    async def run_stage_agent(
        self,
        *,
        stage: str,
        allowed_tools: tuple[str, ...],
        run_tool: StageToolRunner,
        inputs: dict[str, Any],
        target_model: Optional[str] = None,
    ) -> Any:
        """Loop *critique -> refine* bounded, com o modelo servido pelo AI gateway.

        Gera o rascunho pela typed tool (``run_tool``), pede uma crítica acionável e, se
        houver, regenera uma vez com a diretiva. O agent só toca o domínio via
        ``run_tool`` (fronteira D29).
        """
        draft = await run_tool(**inputs)
        revision = await self._agent_critique(stage, draft, model=target_model or self.model)
        add_trace_metadata(
            agent_backend="vercel_gateway",
            stage=stage,
            allowed_tools=list(allowed_tools),
            target_model=target_model,
            agent_revised=bool(revision),
        )
        if not revision:
            return draft
        return await run_tool(**{**inputs, "revision": revision})

    @traced("adapter.gateway.agent_critique", run_type="llm", provider="vercel_gateway")
    async def _agent_critique(self, stage: str, draft: Any, *, model: str) -> Optional[str]:
        """Pede ao modelo uma diretiva de refino do rascunho (ou aprovação).

        Retorna ``None`` quando o modelo aprova (responde ``APPROVE``) ou quando a leitura
        falha — o rascunho já é válido (passou pelos validators da tool). Nunca levanta.
        """
        critique_prompt = (
            f"You are reviewing the draft output of the '{stage}' stage of a UGC ad "
            "pipeline. If the draft is already strong, reply with exactly: APPROVE\n"
            "Otherwise reply with a single actionable one-line revision directive "
            "(no preamble). Draft:\n\n"
            f"{draft!r}"
        )
        try:
            data = await self._chat(
                [{"role": "user", "content": critique_prompt}],
                max_tokens=400,
                model=model,
            )
            directive = self._message_text(data).strip()
        except Exception:
            return None
        if not directive or directive.upper().startswith("APPROVE"):
            return None
        return directive


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
