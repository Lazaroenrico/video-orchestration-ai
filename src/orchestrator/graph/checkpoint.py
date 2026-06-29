"""Checkpointer (resumibilidade). thread_id = run id.

O grafo roda async (``ainvoke``), então o checkpointer precisa expor os métodos
async esperados pelo LangGraph. Usamos o ``SqliteSaver`` síncrono por baixo e uma
fachada async fina porque ``aiosqlite.connect`` trava neste ambiente. O serializador
registra explicitamente os tipos pydantic do estado (``Item``/``Artifact``/
``QCResult``) — sem isso, versões futuras do LangGraph bloqueiam a desserialização
desses tipos do checkpoint.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

_ALLOWED_TYPES = [
    ("orchestrator.graph.state", "Item"),
    ("orchestrator.graph.state", "Artifact"),
    ("orchestrator.graph.state", "QCResult"),
]


def _serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_TYPES)


class AsyncSqliteCompatSaver(SqliteSaver):
    """Async facade over LangGraph's sync SqliteSaver.

    ``SqliteSaver`` is not async-aware, but LangGraph's async runtime only requires
    awaitable checkpoint methods. The operations here are small SQLite calls, so we
    execute them synchronously under a lock instead of using worker threads. This
    avoids the local runtime issue where thread-delivered asyncio futures never
    wake the event loop.
    """

    def __init__(self, conn: sqlite3.Connection, *, serde: JsonPlusSerializer) -> None:
        super().__init__(conn, serde=serde)
        self._lock = threading.RLock()

    def setup(self) -> None:
        with self._lock:
            super().setup()

    async def aget_tuple(self, config: dict[str, Any]) -> Any:
        return self._locked_call(self.get_tuple, config)

    async def alist(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        rows = self._locked_collect(
            self.list, config, filter=filter, before=before, limit=limit
        )
        for row in rows:
            yield row

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> dict[str, Any]:
        return self._locked_call(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._locked_call(self.put_writes, config, writes, task_id, task_path)

    def _locked_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return fn(*args, **kwargs)

    def _locked_collect(self, fn: Any, *args: Any, **kwargs: Any) -> list[Any]:
        with self._lock:
            return list(fn(*args, **kwargs))


@asynccontextmanager
async def open_checkpointer(db_path: str | Path) -> AsyncIterator[AsyncSqliteCompatSaver]:
    """Abre (criando se preciso) o checkpointer persistente num arquivo sqlite."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = AsyncSqliteCompatSaver(conn, serde=_serde())
        saver.setup()
        yield saver
    finally:
        conn.close()
