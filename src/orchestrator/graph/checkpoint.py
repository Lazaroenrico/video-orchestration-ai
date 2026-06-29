"""Checkpointer (resumibilidade). thread_id = run id.

O grafo roda async (``ainvoke``), então usa-se ``AsyncSqliteSaver`` (sobre aiosqlite),
exposto como context manager async. O serializador registra explicitamente os tipos
pydantic do estado (``Item``/``Artifact``/``QCResult``) — sem isso, versões futuras do
LangGraph bloqueiam a desserialização desses tipos do checkpoint.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_ALLOWED_TYPES = [
    ("orchestrator.graph.state", "Item"),
    ("orchestrator.graph.state", "Artifact"),
    ("orchestrator.graph.state", "QCResult"),
]


def _serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_TYPES)


@asynccontextmanager
async def open_checkpointer(db_path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """Abre (criando se preciso) o checkpointer persistente num arquivo sqlite."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    try:
        saver = AsyncSqliteSaver(conn, serde=_serde())
        await saver.setup()
        yield saver
    finally:
        await conn.close()
