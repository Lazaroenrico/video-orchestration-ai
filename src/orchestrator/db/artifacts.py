"""Repositório PostgreSQL de metadados canônicos de artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb

from orchestrator.db.database import Database
from orchestrator.db.tenancy import TenantContext
from orchestrator.storage.db import ArtifactRecord
from orchestrator.storage.retention import expires_at_for


def _to_record(row: tuple[Any, ...]) -> ArtifactRecord:
    expires_at = row[11]
    return ArtifactRecord(
        run_id=row[0],
        item_id=row[1],
        creator_id=row[2],
        kind=row[3],
        storage_backend=row[4],
        storage_key=row[5],
        content_type=row[6],
        size_bytes=row[7],
        sha256=row[8],
        source_uri=row[9],
        retention_class=row[10],
        expires_at=(
            expires_at.astimezone(timezone.utc).isoformat()
            if expires_at is not None
            else None
        ),
        meta=row[12],
    )


_SELECT_FIELDS = """
    run_id, item_id, creator_id, kind, storage_backend, storage_key,
    content_type, size_bytes, sha256, source_uri, retention_class,
    expires_at, meta
"""


class PostgresArtifactRepository:
    """Mantém somente metadata; os bytes continuam no backend de storage."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def record(self, artifact: ArtifactRecord) -> ArtifactRecord:
        async with self._database.connection(self._tenant) as connection:
            await connection.execute(
                """
                INSERT INTO artifacts (
                    organization_id, id, run_id, item_id, creator_id, kind,
                    storage_backend, storage_key, content_type, size_bytes, sha256,
                    source_uri, retention_class, expires_at, meta
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (organization_id, id) DO UPDATE
                SET run_id = EXCLUDED.run_id,
                    item_id = EXCLUDED.item_id,
                    creator_id = EXCLUDED.creator_id,
                    kind = EXCLUDED.kind,
                    storage_backend = EXCLUDED.storage_backend,
                    storage_key = EXCLUDED.storage_key,
                    content_type = EXCLUDED.content_type,
                    size_bytes = EXCLUDED.size_bytes,
                    sha256 = EXCLUDED.sha256,
                    source_uri = EXCLUDED.source_uri,
                    retention_class = EXCLUDED.retention_class,
                    expires_at = EXCLUDED.expires_at,
                    meta = EXCLUDED.meta,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self._tenant.organization_id,
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
                    Jsonb(artifact.meta),
                ),
            )
        return artifact

    async def get(self, artifact_id: str) -> Optional[ArtifactRecord]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM artifacts
                WHERE organization_id = %s AND id = %s
                """,
                (self._tenant.organization_id, artifact_id),
            )
            row = await cursor.fetchone()
        return _to_record(row) if row is not None else None

    async def by_run(self, run_id: str) -> list[ArtifactRecord]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM artifacts
                WHERE organization_id = %s AND run_id = %s
                ORDER BY storage_key
                """,
                (self._tenant.organization_id, run_id),
            )
            rows = await cursor.fetchall()
        return [_to_record(row) for row in rows]

    async def by_key(self, storage_key: str) -> Optional[ArtifactRecord]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM artifacts
                WHERE organization_id = %s AND storage_key = %s
                """,
                (self._tenant.organization_id, storage_key),
            )
            row = await cursor.fetchone()
        return _to_record(row) if row is not None else None

    async def set_retention(
        self,
        storage_key: str,
        retention_class: str,
        *,
        now: datetime,
    ) -> None:
        expires_at = expires_at_for(retention_class, now=now)
        async with self._database.connection(self._tenant) as connection:
            await connection.execute(
                """
                UPDATE artifacts
                SET retention_class = %s,
                    expires_at = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE organization_id = %s AND storage_key = %s
                """,
                (
                    retention_class,
                    expires_at,
                    self._tenant.organization_id,
                    storage_key,
                ),
            )

    async def set_storage_backend(
        self,
        storage_key: str,
        storage_backend: str,
    ) -> None:
        """Troca somente a localização após uma cópia já verificada."""
        async with self._database.connection(self._tenant) as connection:
            await connection.execute(
                """
                UPDATE artifacts
                SET storage_backend = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE organization_id = %s AND storage_key = %s
                """,
                (
                    storage_backend,
                    self._tenant.organization_id,
                    storage_key,
                ),
            )

    async def expired(self, *, now: datetime) -> list[ArtifactRecord]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM artifacts
                WHERE organization_id = %s
                  AND expires_at IS NOT NULL
                  AND expires_at <= %s
                ORDER BY storage_key
                """,
                (self._tenant.organization_id, now),
            )
            rows = await cursor.fetchall()
        return [_to_record(row) for row in rows]

    async def delete(self, artifact_id: str) -> None:
        async with self._database.connection(self._tenant) as connection:
            await connection.execute(
                """
                DELETE FROM artifacts
                WHERE organization_id = %s AND id = %s
                """,
                (self._tenant.organization_id, artifact_id),
            )
