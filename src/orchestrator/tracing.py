"""orchestrator/tracing.py — helper centralizado de tracing LangSmith.

Import-safe: no-op completo se ``langsmith`` estiver ausente ou
``LANGSMITH_TRACING`` estiver off. Zero mudança de comportamento offline.

Segurança: ``process_inputs=_drop_sensitive_inputs`` remove ``self``, ``config``,
clients httpx e TOKENS/Authorization dos spans.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import os
from typing import Any, Callable, Optional

try:
    from langsmith import traceable as _ls_traceable
    from langsmith.run_helpers import get_current_run_tree
    from langsmith.wrappers import wrap_anthropic as _ls_wrap_anthropic
    _HAS_LS = True
except Exception:  # lib ausente ou falha de import → tudo vira no-op
    _HAS_LS = False


_LLM_PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-4-8":  {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write_5m": 6.25},
    "claude-sonnet-5":  {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write_5m": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0,  "cache_read": 0.10, "cache_write_5m": 1.25},
}

_TRUTHY = {"1", "true", "yes", "on"}
_DROP_KEYS = {
    "self",
    "config",
    "client",
    "headers",
    "authorization",
    "auth",
    "token",
    "api_key",
    "password",
    "secret",
}
_REDACT_KEYS = {
    "offer",
    "concept",
    "script",
    "hook",
    "angle",
    "prompt",
    "system_prompt",
    "video_prompt",
    "creator_prompt",
    "messages",
    "image_url",
    "primary",
    "upscaled_base",
    "url",
    "uri",
    "output_url",
    "voice_id",
}
_MAX_TRACE_STRING = 256


def is_tracing_enabled() -> bool:
    """Retorna o gate runtime de tracing.

    O CLI carrega ``.env`` depois do import dos módulos; por isso o gate precisa
    ler o ambiente no momento de uso, não no import.
    """
    return os.environ.get("LANGSMITH_TRACING", "").strip().lower() in _TRUTHY


def _is_sensitive_string(value: str) -> bool:
    lower = value.lower()
    return (
        lower.startswith("data:")
        or "base64," in lower
        or len(value) > _MAX_TRACE_STRING
    )


def _sanitize_trace_payload(payload: Any, *, key: str = "") -> Any:
    """Reduz payloads de trace para metadata segura e pequena."""
    key_l = key.lower()
    if key_l in _REDACT_KEYS:
        return "<redacted>"
    if isinstance(payload, dict):
        return {
            k: _sanitize_trace_payload(v, key=str(k))
            for k, v in payload.items()
            if str(k).lower() not in _DROP_KEYS
        }
    if isinstance(payload, (list, tuple)):
        return [_sanitize_trace_payload(v) for v in payload[:20]]
    if isinstance(payload, str):
        return "<redacted>" if _is_sensitive_string(payload) else payload
    return payload


def _drop_sensitive_inputs(inputs: dict) -> dict:
    """Evita serializar clients, configs, tokens e prompts em spans LangSmith."""
    clean: dict[str, Any] = {}
    for key, value in inputs.items():
        key_l = str(key).lower()
        if key_l in _DROP_KEYS:
            continue
        clean[key] = _sanitize_trace_payload(value, key=str(key))
    return clean


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _mark_traceable(fn: Callable, name: str, run_type: str, metadata: dict[str, Any]) -> Callable:
    """Anexa marcadores offline para testes de cobertura de tracing."""
    setattr(fn, "__trace_name__", name)
    setattr(fn, "__trace_run_type__", run_type)
    setattr(fn, "__trace_metadata__", dict(metadata))
    return fn


def traced(name: Optional[str] = None, run_type: str = "chain", **metadata) -> Callable:
    """Decorator de span LangSmith.

    Preserva assinatura e natureza async via ``functools.wraps`` →
    Protocols runtime_checkable continuam batendo. Exclui inputs sensíveis.
    Quando LangSmith está ausente ou tracing off, é passthrough puro.
    """
    def deco(fn: Callable) -> Callable:
        trace_name = name or fn.__name__
        cached: dict[str, Callable] = {}

        def _langsmith_wrapped() -> Callable:
            if "fn" not in cached:
                cached["fn"] = _ls_traceable(
                    name=trace_name,
                    run_type=run_type,
                    metadata=metadata or None,
                    process_inputs=_drop_sensitive_inputs,
                    process_outputs=_sanitize_trace_payload,
                )(fn)
            return cached["fn"]

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if _HAS_LS and is_tracing_enabled():
                    return await _langsmith_wrapped()(*args, **kwargs)
                return await fn(*args, **kwargs)

            return _mark_traceable(async_wrapper, trace_name, run_type, metadata)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _HAS_LS and is_tracing_enabled():
                return _langsmith_wrapped()(*args, **kwargs)
            return fn(*args, **kwargs)

        return _mark_traceable(wrapper, trace_name, run_type, metadata)
    return deco


def wrap_anthropic_client(client: Any) -> Any:
    """Envolve AsyncAnthropic para spans LLM com token usage/custo.

    Passthrough se LangSmith estiver ausente — devolve o próprio ``client``.
    """
    if _HAS_LS and is_tracing_enabled():
        return _ls_wrap_anthropic(client)
    return client


def _normalize_model(model: str) -> str:
    """Normaliza um model id para a chave usada em ``_LLM_PRICES_PER_MTOK``.

    - Corta prefixo de provider/gateway (ex.: ``"anthropic/claude-opus-4.8"``
      -> ``"claude-opus-4.8"``), pegando a última parte após ``/``.
    - Converte pontos em traços (ex.: ``"claude-opus-4.8"`` -> ``"claude-opus-4-8"``).
    - Idempotente para ids já normalizados.
    """
    name = model.rsplit("/", 1)[-1]
    return name.replace(".", "-")


def compute_llm_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Optional[dict[str, float]]:
    """Calcula custo USD a partir da tabela de preços local.

    Retorna ``None`` se o modelo (normalizado) não estiver na tabela — nesse
    caso o chamador simplesmente não anexa custo (sem levantar exceção).
    """
    prices = _LLM_PRICES_PER_MTOK.get(_normalize_model(model))
    if prices is None:
        return None

    input_cost = (
        input_tokens * prices["input"]
        + cache_read_tokens * prices["cache_read"]
        + cache_write_tokens * prices["cache_write_5m"]
    ) / 1_000_000
    output_cost = output_tokens * prices["output"] / 1_000_000

    return {
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": input_cost + output_cost,
    }


def _usage_get(usage: Any, key: str) -> int:
    """Lê um campo de ``usage`` (objeto do SDK ou dict), com default 0."""
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    return value if value is not None else 0


def build_usage_metadata(usage: Any, model: str) -> dict[str, Any]:
    """Monta ``usage_metadata`` (tokens + custo) a partir de ``response.usage``.

    Tokens de cache são aditivos ao input (espelha o formato usado pelo
    LangSmith): ``total_input = input_tokens + cache_read + cache_write``.
    """
    input_tokens = _usage_get(usage, "input_tokens")
    output_tokens = _usage_get(usage, "output_tokens")
    cache_read = _usage_get(usage, "cache_read_input_tokens")
    cache_write = _usage_get(usage, "cache_creation_input_tokens")

    total_input = input_tokens + cache_read + cache_write

    metadata: dict[str, Any] = {
        "input_tokens": total_input,
        "output_tokens": output_tokens,
        "total_tokens": total_input + output_tokens,
    }

    details = {k: v for k, v in (("cache_read", cache_read), ("cache_creation", cache_write)) if v}
    if details:
        metadata["input_token_details"] = details

    cost = compute_llm_cost(model, input_tokens, output_tokens, cache_read, cache_write)
    if cost is not None:
        metadata["input_cost"] = cost["input_cost"]
        metadata["output_cost"] = cost["output_cost"]
        metadata["total_cost"] = cost["total_cost"]

    return metadata


def record_llm_usage(usage: Any, model: str) -> None:
    """Anexa ``usage_metadata`` (tokens + custo) à run LangSmith atual.

    No-op se LangSmith estiver ausente, tracing desligado, ou não houver run
    ativa. Nunca lança exceção — é chamado no meio de chamadas de API reais.
    """
    if not _HAS_LS or not is_tracing_enabled():
        return
    try:
        rt = get_current_run_tree()
        if rt is not None:
            rt.metadata["usage_metadata"] = build_usage_metadata(usage, model)
            rt.metadata["ls_model_name"] = _normalize_model(model)
    except Exception:  # noqa: BLE001
        pass


def add_trace_metadata(**kw: Any) -> None:
    """Anexa metadata ao span LangSmith atual.

    No-op se:
    - LangSmith não estiver disponível.
    - Não houver run ativo (ex.: CI offline, chamada fora de span).
    Nunca lança exceção.
    """
    if not _HAS_LS or not is_tracing_enabled():
        return
    try:
        rt = get_current_run_tree()
        if rt is not None:
            rt.metadata.update(_sanitize_trace_payload(kw))
    except Exception:  # noqa: BLE001
        pass


def run_trace_config(
    run_id: str,
    *,
    offer: Optional[str] = None,
    platform: Optional[str] = None,
    batch: Optional[int] = None,
) -> dict[str, Any]:
    """Campos de trace para mesclar no cfg do grafo (root run).

    Retorna ``run_name``, ``tags`` e ``metadata`` que o LangGraph aplica ao
    root trace quando mesclados no cfg de topo.
    """
    tags = [
        t for t in [
            platform,
            f"offer_hash:{_hash_text(offer)}" if offer else None,
            f"batch:{batch}" if batch else None,
        ]
        if t is not None
    ]
    metadata = {
        "run_id": run_id,
        "platform": platform,
    }
    if offer:
        metadata["offer_hash"] = _hash_text(offer)
    return {
        "run_name": f"ugc-run:{run_id}",
        "tags": tags,
        "metadata": metadata,
    }
