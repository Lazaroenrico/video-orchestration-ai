"""Checkpointer (resumibilidade). thread_id = run id.

O grafo roda async (``ainvoke``), então o checkpointer precisa expor os métodos
async esperados pelo LangGraph. Usamos o ``SqliteSaver`` síncrono por baixo e uma
fachada async fina porque ``aiosqlite.connect`` trava neste ambiente. O serializador
registra explicitamente os tipos pydantic do estado (``Item``/``Artifact``/
``QCResult``) — sem isso, versões futuras do LangGraph bloqueiam a desserialização
desses tipos do checkpoint.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
)
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from orchestrator.db.tenancy import TenantIdentity

_ALLOWED_TYPES = [
    ("orchestrator.graph.state", "Item"),
    ("orchestrator.graph.state", "Artifact"),
    ("orchestrator.graph.state", "QCResult"),
]
_POSTGRES_CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


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


class TenantScopedPostgresSaver(BaseCheckpointSaver):
    """Mantém ``thread_id`` público e isola a chave física por organização."""

    def __init__(self, saver: AsyncPostgresSaver, organization_id: str) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver
        self._prefix = f"{organization_id}:"

    def _scope_thread(self, thread_id: str) -> str:
        return f"{self._prefix}{thread_id}"

    def _scope_config(self, config: RunnableConfig) -> RunnableConfig:
        configurable = dict(config.get("configurable") or {})
        configurable["thread_id"] = self._scope_thread(str(configurable["thread_id"]))
        return {**config, "configurable": configurable}

    def _external_config(self, config: RunnableConfig) -> RunnableConfig:
        configurable = dict(config.get("configurable") or {})
        thread_id = str(configurable["thread_id"])
        assert thread_id.startswith(self._prefix), (
            "checkpoint fora do escopo da organização atual"
        )
        configurable["thread_id"] = thread_id.removeprefix(self._prefix)
        return {**config, "configurable": configurable}

    def _external_tuple(self, value: CheckpointTuple) -> CheckpointTuple:
        return CheckpointTuple(
            config=self._external_config(value.config),
            checkpoint=value.checkpoint,
            metadata=value.metadata,
            parent_config=(
                self._external_config(value.parent_config)
                if value.parent_config is not None
                else None
            ),
            pending_writes=value.pending_writes,
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        value = await self._saver.aget_tuple(self._scope_config(config))
        return self._external_tuple(value) if value is not None else None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        scoped_config = self._scope_config(config) if config is not None else None
        scoped_before = self._scope_config(before) if before is not None else None
        async for value in self._saver.alist(
            scoped_config,
            filter=filter,
            before=scoped_before,
            limit=limit,
        ):
            if str(value.config["configurable"]["thread_id"]).startswith(self._prefix):
                yield self._external_tuple(value)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        saved = await self._saver.aput(
            self._scope_config(config),
            checkpoint,
            metadata,
            new_versions,
        )
        return self._external_config(saved)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._saver.aput_writes(
            self._scope_config(config),
            writes,
            task_id,
            task_path,
        )

    async def adelete_thread(self, thread_id: str) -> None:
        await self._saver.adelete_thread(self._scope_thread(thread_id))

    async def aget_delta_channel_history(
        self,
        *,
        config: RunnableConfig,
        channels: Sequence[str],
    ) -> Mapping[str, DeltaChannelHistory]:
        return await self._saver.aget_delta_channel_history(
            config=self._scope_config(config),
            channels=channels,
        )

    def get_next_version(self, current: Any, channel: None) -> Any:
        return self._saver.get_next_version(current, channel)


def _install_checkpoint_security(saver: PostgresSaver) -> None:
    connection = saver.conn
    for table in _POSTGRES_CHECKPOINT_TABLES:
        policy = f"{table}_tenant_isolation"
        connection.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        connection.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        cursor = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_policies
                WHERE schemaname = current_schema()
                  AND tablename = %s
                  AND policyname = %s
            ) AS policy_exists
            """,
            (table, policy),
        )
        row = cursor.fetchone()
        if not row["policy_exists"]:
            connection.execute(
                f"""
                CREATE POLICY {policy} ON {table}
                USING (
                    split_part(thread_id, ':', 1) =
                    current_setting('app.organization_id', true)
                )
                WITH CHECK (
                    split_part(thread_id, ':', 1) =
                    current_setting('app.organization_id', true)
                )
                """
            )


def setup_postgres_checkpointer(database_url: str) -> None:
    """Materializa tabelas oficiais do saver e aplica a barreira RLS."""
    conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with PostgresSaver.from_conn_string(conninfo) as saver:
        saver.setup()
        _install_checkpoint_security(saver)


async def _set_checkpoint_tenant(
    saver: AsyncPostgresSaver,
    organization_id: str,
) -> None:
    await saver.conn.execute(
        "SELECT set_config('app.organization_id', %s, false)",
        (organization_id,),
    )


@asynccontextmanager
async def open_checkpointer(db_path: str | Path) -> AsyncIterator[Any]:
    """Seleciona PostgreSQL quando configurado; mantém SQLite no modo local."""
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        async with AsyncPostgresSaver.from_conn_string(
            database_url,
            serde=_serde(),
        ) as saver:
            tenant = TenantIdentity.from_env().context()
            await _set_checkpoint_tenant(saver, str(tenant.organization_id))
            yield TenantScopedPostgresSaver(saver, str(tenant.organization_id))
        return

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        saver = AsyncSqliteCompatSaver(conn, serde=_serde())
        saver.setup()
        yield saver
    finally:
        conn.close()
