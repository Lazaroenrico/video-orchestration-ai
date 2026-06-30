"""Retry de transporte para adapters que falam HTTP com APIs externas.

Blips de conexão (``ConnectTimeout``/``ConnectError``) são intermitentes — retentar
com backoff resolve a maioria. HTTP status errors (401/422/500) e erros de lógica
(``RuntimeError`` etc.) NÃO são retentados: propagam na hora, porque retentar não
ajudaria e mascararia o problema real.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

_log = logging.getLogger(__name__)


async def with_transport_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    label: str = "replicate",
) -> Any:
    """Chama ``fn()`` retentando só em ``httpx.TransportError``.

    ``httpx.TransportError`` cobre ConnectTimeout, ConnectError, ReadTimeout e
    PoolTimeout — e exclui ``HTTPStatusError`` (4xx/5xx) e qualquer outra exceção.
    Backoff exponencial determinístico: ``backoff_base * 2**attempt`` (``0`` =
    instantâneo, usado nos testes). Sem jitter (determinismo, CLAUDE.md).
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(backoff_base * (2 ** attempt))
            _log.warning(
                "%s transporte falhou (%d/%d): %s; retry",
                label, attempt + 1, max_retries + 1, type(exc).__name__,
            )
            attempt += 1
