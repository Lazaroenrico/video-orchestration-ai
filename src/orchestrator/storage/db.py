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

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from orchestrator.storage.base import StoredObject

# Retenção da D30. ``keep`` é o default seguro: um artifact só expira se alguém
# declarar explicitamente que ele é descartável.
RETENTION_KEEP = "keep"
RETENTION_SHORT_LIVED = "short_lived"

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
    "id", "run_id", "item_id", "creator_id", "kind", "storage_backend", "storage_key",
    "content_type", "size_bytes", "sha256", "source_uri", "retention_class",
    "expires_at", "meta_json",
)


@dataclass(frozen=True)
class ArtifactRecord:
    """Uma linha canônica de artifact. Espelha as colunas mínimas da ADR-D30."""

    run_id: str
    kind: str
    storage_backend: str
    storage_key: str
    item_id: Optional[str] = None
    creator_id: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    source_uri: Optional[str] = None
    retention_class: str = RETENTION_KEEP
    expires_at: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Id determinístico derivado do ponteiro canônico.

        Determinismo (CLAUDE.md): nada de ``uuid4``. Como ``storage_key`` já é único por
        run/item/kind, derivar o id dele torna ``record`` idempotente de graça — um
        retry que re-persiste os mesmos bytes atualiza a linha em vez de duplicá-la.
        """
        return hashlib.sha256(f"{self.run_id}:{self.storage_key}".encode()).hexdigest()[:32]

    @classmethod
    def from_stored(
        cls,
        stored: StoredObject,
        *,
        run_id: str,
        kind: str,
        item_id: Optional[str] = None,
        creator_id: Optional[str] = None,
        source_uri: Optional[str] = None,
        retention_class: str = RETENTION_KEEP,
        expires_at: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> "ArtifactRecord":
        """Ponte com a Fase 1: o ``StoredObject`` já traz backend/key/hash/tamanho."""
        return cls(
            run_id=run_id,
            kind=kind,
            storage_backend=stored.backend,
            storage_key=stored.key,
            item_id=item_id,
            creator_id=creator_id,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            source_uri=source_uri,
            retention_class=retention_class,
            expires_at=expires_at,
            meta=meta or {},
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
                "SELECT * FROM artifacts WHERE storage_key = ?", (storage_key,),
            ).fetchone()
        return _to_record(row) if row else None

    async def by_run(self, run_id: str) -> list[ArtifactRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY storage_key", (run_id,),
            ).fetchall()
        return [_to_record(row) for row in rows]
