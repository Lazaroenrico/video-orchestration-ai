"""Retry de transporte para adapters que falam HTTP com APIs externas.

Blips de conexão (``ConnectTimeout``/``ConnectError``) são intermitentes — retentar
com backoff resolve a maioria. O Replicate também throttla com ``429`` quando a conta
tem crédito baixo (burst reduzido); esse status é transitório ("resets in ~Ns") e
igualmente retentável. Adapters httpx puros (ex.: ElevenLabs) sinalizam o mesmo
throttle via ``raise_for_status`` -> ``httpx.HTTPStatusError`` com
``response.status_code == 429``; tratamos igual. HTTP status errors não-429
(401/422/500) e erros de lógica (``RuntimeError`` etc.) NÃO são retentados: propagam
na hora, porque retentar não ajudaria e mascararia o problema real.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

import httpx
from replicate.exceptions import ReplicateError

_log = logging.getLogger(__name__)

# O corpo do 429 do Replicate diz quando o budget volta, em duas variações:
# "resets in ~8s" e "Expected available in 3 seconds".
_RESET_HINT = re.compile(
    r"(?:resets in|available in)\s*~?\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)


def _throttle_reset_hint(exc: BaseException) -> Optional[float]:
    """Extrai (em segundos) o hint de reset de um erro 429, se presente."""
    detail = getattr(exc, "detail", None)
    message = detail if isinstance(detail, str) else str(exc)
    match = _RESET_HINT.search(message or "")
    return float(match.group(1)) if match else None


# Erros de transporte PRÉ-envio: a request nunca chegou ao provedor, então
# retentar é seguro. ``ReadTimeout``/``WriteError`` ficam de fora de propósito:
# são pós-envio — a prediction pode já ter iniciado e retentar recriaria a
# operação (dupla cobrança, crítico no vídeo).
_PRE_SEND_TRANSPORT_ERRORS = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
)


def _is_retryable(exc: BaseException) -> bool:
    """True para erros transitórios: blips de conexão pré-envio e throttle 429.

    Só retenta erros de transporte em que a request comprovadamente não chegou
    ao provedor (``ConnectTimeout``, ``ConnectError``, ``PoolTimeout``).
    ``ReadTimeout`` NÃO é retentável: a operação pode já ter iniciado.
    ``ReplicateError`` só é retentável quando ``status == 429`` (throttle);
    ``HTTPStatusError`` também só é retentável quando a resposta é ``429``.
    Outros status propagam.
    """
    if isinstance(exc, _PRE_SEND_TRANSPORT_ERRORS):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    if isinstance(exc, ReplicateError):
        return getattr(exc, "status", None) == 429
    return False


async def with_transport_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    max_delay: float = 60.0,
    label: str = "replicate",
) -> Any:
    """Chama ``fn()`` retentando em erros transitórios (conexão + throttle 429).

    Retenta erros de transporte pré-envio (``ConnectTimeout``, ``ConnectError``,
    ``PoolTimeout``), ``ReplicateError`` com ``status == 429`` e
    ``HTTPStatusError`` com ``response.status_code == 429``. Demais exceções —
    ``ReadTimeout`` (pós-envio, não idempotente), ``HTTPStatusError`` não-429,
    ``RuntimeError`` etc. — propagam na hora. Backoff exponencial
    determinístico: ``backoff_base * 2**attempt`` (``0`` = instantâneo, usado nos
    testes). Quando o 429 traz o hint de reset ("resets in ~8s"), a espera é no
    mínimo o hint + 1s de folga — retentar antes disso é 429 garantido. O hint
    vem do corpo da resposta (externo, não confiável) e o backoff cresce sem
    limite, então todo delay é capado em ``max_delay``. Sem jitter
    (determinismo, CLAUDE.md).
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            if not _is_retryable(exc) or attempt >= max_retries:
                raise
            delay = backoff_base * (2 ** attempt)
            hint = _throttle_reset_hint(exc)
            if hint is not None:
                delay = max(delay, hint + 1.0)
            delay = min(delay, max_delay)
            await asyncio.sleep(delay)
            _log.warning(
                "%s falhou (%d/%d): %s; retry",
                label, attempt + 1, max_retries + 1, type(exc).__name__,
            )
            attempt += 1
