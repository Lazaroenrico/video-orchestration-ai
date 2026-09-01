"""DB relacional de artifacts — fonte canônica de metadata e ponteiros (D30).

O storage guarda **bytes**; este DB guarda **verdade**: quem produziu o artifact, onde
ele está (``storage_backend`` + ``storage_key``), integridade (``sha256``,
``size_bytes``), proveniência (``source_uri``) e retenção (``retention_class``,
``expires_at``). Signed URLs são derivadas de ``storage_key`` sob demanda e **nunca**
persistidas aqui — uma URL expirada não pode virar a verdade de onde o objeto está.

SQLite-first (D30) para preservar o modo offline: sem credencial, sem rede, sem custo.

Concorrência: usamos ``sqlite3`` síncrono sob um lock, com fachada async — o mesmo
padrão (e pelo mesmo motivo) de ``graph/checkpoint.py``, onde ``aiosqlite.connect``
trava neste ambiente. As operações são pequenas o bastante para não bloquear o loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional, Protocol

from orchestrator.storage.records import ArtifactRecord
from orchestrator.storage.retention import expires_at_for

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    item_id         TEXT,
    creator_id      TEXT,
    kind            TEXT NOT NULL,
    storage_backend TEXT NOT NULL,
    storage_key     TEXT NOT NULL,
    content_type    TEXT,
    size_bytes      INTEGER,
    sha256          TEXT,
    source_uri      TEXT,
    retention_class TEXT NOT NULL,
    expires_at      TEXT,
    meta_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_key ON artifacts(storage_key);
CREATE INDEX IF NOT EXISTS idx_artifacts_expires ON artifacts(expires_at);
"""

_COLUMNS = (
    "id",
    "run_id",
    "item_id",
    "creator_id",
    "kind",
    "storage_backend",
    "storage_key",
    "content_type",
    "size_bytes",
    "sha256",
    "source_uri",
    "retention_class",
    "expires_at",
    "meta_json",
)


def _to_record(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        run_id=row["run_id"],
        kind=row["kind"],
        storage_backend=row["storage_backend"],
        storage_key=row["storage_key"],
        item_id=row["item_id"],
        creator_id=row["creator_id"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        source_uri=row["source_uri"],
        retention_class=row["retention_class"],
        expires_at=row["expires_at"],
        meta=json.loads(row["meta_json"]),
    )


class ArtifactRepository(Protocol):
    """Contrato compartilhado pelos backends SQLite e PostgreSQL."""

    async def record(self, artifact: ArtifactRecord) -> ArtifactRecord: ...

    async def get(self, artifact_id: str) -> Optional[ArtifactRecord]: ...

    async def by_key(self, storage_key: str) -> Optional[ArtifactRecord]: ...

    async def by_run(self, run_id: str) -> list[ArtifactRecord]: ...

    async def set_retention(
        self,
        storage_key: str,
        retention_class: str,
        *,
        now: datetime,
    ) -> None: ...

    async def expired(self, *, now: datetime) -> list[ArtifactRecord]: ...

    async def delete(self, artifact_id: str) -> None: ...


class ArtifactDB:
    """Fonte canônica de artifacts. Fachada async sobre ``sqlite3`` síncrono."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def setup(self) -> None:
        """Cria schema e diretório. Idempotente: startup repetido é normal."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def record(self, artifact: ArtifactRecord) -> ArtifactRecord:
        """Grava (ou atualiza) o artifact. Idempotente pelo id determinístico."""
        values = (
            artifact.id,
            artifact.run_id,
            artifact.item_id,
            artifact.creator_id,
            artifact.kind,
            artifact.storage_backend,
            artifact.storage_key,
            artifact.content_type,
            artifact.size_bytes,
            artifact.sha256,
            artifact.source_uri,
            artifact.retention_class,
            artifact.expires_at,
            json.dumps(artifact.meta, sort_keys=True),
        )
        placeholders = ", ".join("?" * len(_COLUMNS))
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO artifacts ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
        return artifact

    async def get(self, artifact_id: str) -> Optional[ArtifactRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return _to_record(row) if row else None

    async def by_key(self, storage_key: str) -> Optional[ArtifactRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE storage_key = ?",
                (storage_key,),
            ).fetchone()
        return _to_record(row) if row else None

    async def by_run(self, run_id: str) -> list[ArtifactRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY storage_key",
                (run_id,),
            ).fetchall()
        return [_to_record(row) for row in rows]

    async def set_retention(self, storage_key: str, retention_class: str, *, now: datetime) -> None:
        """Reclassifica um artifact e recalcula seu ``expires_at``.

        Existe porque a classe só é conhecida **depois** do fato: quando o clip é
        persistido o QC ainda não rodou, então não dá para saber se ele será aprovado,
        superado por outra take, ou reprovado. Key desconhecida é no-op.
        """
        expires_at = expires_at_for(retention_class, now=now)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE artifacts SET retention_class = ?, expires_at = ? WHERE storage_key = ?",
                (retention_class, expires_at, storage_key),
            )

    async def expired(self, *, now: datetime) -> list[ArtifactRecord]:
        """Artifacts já vencidos. ``expires_at IS NULL`` (retido) nunca entra."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE expires_at IS NOT NULL AND expires_at <= ? "
                "ORDER BY storage_key",
                (now.isoformat(),),
            ).fetchall()
        return [_to_record(row) for row in rows]

    async def delete(self, artifact_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))


@asynccontextmanager
async def open_artifact_repository(
    path: str | Path,
    *,
    postgres_factory: Optional[Callable[[Any, Any], ArtifactRepository]] = None,
) -> AsyncIterator[ArtifactRepository]:
    """Seleciona PostgreSQL por ``DATABASE_URL``; sem ela, mantém SQLite local.

    ``postgres_factory`` é a costura de inversão: a composition root (ex.: a API ou
    o runner) pode injetar a construção do repositório PostgreSQL sem que este
    módulo conheça ``orchestrator.db``. Quando omitida, mantém o fallback histórico
    de import tardio para não quebrar chamadores existentes.
    """
    from orchestrator.runtime_mode import open_repository_backend

    def local_repository() -> ArtifactDB:
        repository = ArtifactDB(path)
        repository.setup()
        return repository

    if postgres_factory is None:

        def postgres_repository(database, tenant):
            from orchestrator.db import PostgresArtifactRepository

            return PostgresArtifactRepository(database, tenant)

    else:
        postgres_repository = postgres_factory

    async with open_repository_backend(local_repository, postgres_repository) as repository:
        yield repository
