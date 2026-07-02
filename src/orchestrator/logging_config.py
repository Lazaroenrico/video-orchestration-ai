"""Configuração central de logging do orquestrador.

Sem isto, os ``_log.error(...)`` dos caminhos best-effort (upscale/voz/roster,
preview de voz) caem no *last resort* do Python: stderr, sem timestamp, sem nome
do logger, sem nível controlável — daí "muitos erros que não sei o que são".

``configure_logging`` é idempotente (seguro chamar em cada entrypoint) e lê:
- ``ORCHESTRATOR_LOG_LEVEL`` — nível do root (default ``INFO``).
- ``ORCHESTRATOR_LOG_FILE``  — se setado, também escreve num arquivo.
"""
from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def _resolve_level(raw: str | None) -> int:
    """Converte o nome do nível em constante do logging; default INFO se inválido."""
    if not raw:
        return logging.INFO
    level = logging.getLevelName(raw.strip().upper())
    return level if isinstance(level, int) else logging.INFO


def _make_handler(handler: logging.Handler) -> logging.Handler:
    """Marca e formata um handler para ownership/idempotência."""
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler._orchestrator_handler = True  # type: ignore[attr-defined]
    return handler


def configure_logging() -> None:
    """Configura o root logger uma única vez (idempotente).

    Chamável em todo entrypoint (CLI, web). Remove handlers próprios de chamadas
    anteriores antes de re-adicionar, evitando saída duplicada.
    """
    root = logging.getLogger()
    level = _resolve_level(os.environ.get("ORCHESTRATOR_LOG_LEVEL"))
    root.setLevel(level)

    # Remove handlers que nós mesmos instalamos antes (idempotência).
    for h in [h for h in root.handlers if getattr(h, "_orchestrator_handler", False)]:
        root.removeHandler(h)

    root.addHandler(_make_handler(logging.StreamHandler()))

    log_file = os.environ.get("ORCHESTRATOR_LOG_FILE")
    if log_file:
        root.addHandler(_make_handler(logging.FileHandler(log_file)))
