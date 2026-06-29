"""Bus de streaming de tokens LLM para a UI web.

Usa um ContextVar Python — herdado por tasks filho do asyncio (fan-out paralelo)
sem precisar de lock. O callback é injetado pelo servidor web antes de ainvoke()
e removido ao final do run. Quando nenhum callback está registrado, emit_token()
é no-op — nenhuma mudança de comportamento para testes/CLI.
"""
from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

_TOKEN_CB: contextvars.ContextVar[Optional[Callable[[dict[str, Any]], None]]] = (
    contextvars.ContextVar("_token_cb", default=None)
)


def set_token_callback(cb: Callable[[dict[str, Any]], None]) -> None:
    _TOKEN_CB.set(cb)


def clear_token_callback() -> None:
    _TOKEN_CB.set(None)


def is_streaming() -> bool:
    return _TOKEN_CB.get() is not None


def emit_token(event: dict[str, Any]) -> None:
    cb = _TOKEN_CB.get()
    if cb is not None:
        cb(event)
