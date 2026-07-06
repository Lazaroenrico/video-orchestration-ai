"""Rate-limiter global para chamadas Replicate.

Contas Replicate com crédito baixo (<US$5) operam com cap de ~6 req/min e burst 1.
O roster dispara N creators em paralelo, cada um com upscale + voz (e depois vídeo),
estourando o burst instantaneamente — todo mundo leva ``429``. Este módulo impõe:

- **Serialização**: no máximo ``concurrency`` chamadas Replicate em voo (default 1).
- **Espaçamento**: intervalo mínimo entre *inícios* de chamada (default 10s ≈ 6/min).

O throttle é um singleton de processo (``get_replicate_throttle``) compartilhado por
TODOS os adapters Replicate (voz, upscale, vídeo) — eles dividem o mesmo orçamento de
rate limit da conta. Com crédito >US$5 na conta, zere via env::

    REPLICATE_MIN_INTERVAL_SECONDS=0
    REPLICATE_MAX_CONCURRENCY=8

Determinismo (CLAUDE.md): ``clock`` e ``sleep`` são injetáveis; os testes usam clock
fake e nunca dormem de verdade.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

_log = logging.getLogger(__name__)


class AsyncThrottle:
    """Semáforo + espaçamento mínimo entre inícios de chamada.

    A reserva do próximo slot é feita de forma síncrona (sem await) logo após
    adquirir o semáforo, então não há corrida entre tasks do mesmo loop.
    O semáforo é criado preguiçosamente e refeito se o event loop mudar
    (cada teste roda em loop próprio; produção usa um único loop).
    """

    def __init__(
        self,
        min_interval: float = 0.0,
        concurrency: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval = float(min_interval)
        self.concurrency = max(1, int(concurrency))
        self._clock = clock
        self._sleep = sleep
        self._next_start: Optional[float] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_loop: Optional[asyncio.AbstractEventLoop] = None

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self.concurrency)
            self._sem_loop = loop
        return self._sem

    async def run(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Executa ``fn()`` respeitando concorrência e intervalo mínimo."""
        async with self._semaphore():
            now = self._clock()
            start = now
            if self._next_start is not None and self._next_start > now:
                start = self._next_start
            self._next_start = start + self.min_interval
            if start > now:
                await self._sleep(start - now)
            return await fn()


_GLOBAL: Optional[AsyncThrottle] = None


def _env_number(name: str, default: float, cast: Callable[[str], Any]) -> Any:
    """Lê env numérica com fallback: um typo de configuração não pode derrubar a
    primeira chamada Replicate do pipeline inteiro."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        _log.warning(
            "%s=%r inválido; usando default %s", name, raw, default
        )
        return default


def get_replicate_throttle() -> AsyncThrottle:
    """Singleton de processo — todos os adapters Replicate dividem este orçamento."""
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = AsyncThrottle(
            min_interval=_env_number("REPLICATE_MIN_INTERVAL_SECONDS", 10.0, float),
            concurrency=_env_number("REPLICATE_MAX_CONCURRENCY", 1, int),
        )
    return _GLOBAL
