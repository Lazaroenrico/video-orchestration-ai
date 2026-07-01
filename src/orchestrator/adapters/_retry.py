"""Retry de transporte para adapters que falam HTTP com APIs externas.

Blips de conexão (``ConnectTimeout``/``ConnectError``) são intermitentes — retentar
com backoff resolve a maioria. O Replicate também throttla com ``429`` quando a conta
tem crédito baixo (burst reduzido); esse status é transitório ("resets in ~Ns") e
igualmente retentável. HTTP status errors não-429 (401/422/500) e erros de lógica
(``RuntimeError`` etc.) NÃO são retentados: propagam na hora, porque retentar não
ajudaria e mascararia o problema real.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx
from replicate.exceptions import ReplicateError

_log = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """True para erros transitórios: blips de transporte e throttle 429 do Replicate.

    ``httpx.TransportError`` cobre ConnectTimeout, ConnectError, ReadTimeout e
    PoolTimeout (exclui ``HTTPStatusError`` 4xx/5xx). ``ReplicateError`` só é
    retentável quando ``status == 429`` (throttle); outros status propagam.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, ReplicateError):
        return getattr(exc, "status", None) == 429
    return False


async def with_transport_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    label: str = "replicate",
) -> Any:
    """Chama ``fn()`` retentando em erros transitórios (transporte + 429 do Replicate).

    Retenta ``httpx.TransportError`` (ConnectTimeout, ConnectError, ReadTimeout,
    PoolTimeout) e ``ReplicateError`` com ``status == 429`` (throttle por crédito
    baixo). Demais exceções — ``HTTPStatusError`` não-429, ``RuntimeError`` etc. —
    propagam na hora. Backoff exponencial determinístico: ``backoff_base * 2**attempt``
    (``0`` = instantâneo, usado nos testes). Sem jitter (determinismo, CLAUDE.md).
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            if not _is_retryable(exc) or attempt >= max_retries:
                raise
            await asyncio.sleep(backoff_base * (2 ** attempt))
            _log.warning(
                "%s falhou (%d/%d): %s; retry",
                label, attempt + 1, max_retries + 1, type(exc).__name__,
            )
            attempt += 1
