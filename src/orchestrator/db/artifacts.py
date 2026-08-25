"""Repositório PostgreSQL de metadados canônicos de artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.database import Database
from orchestrator.db.models import Artifact as ArtifactModel
from orchestrator.db.tenancy import TenantContext
from orchestrator.storage.records import ArtifactRecord
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
            expires_at.astimezone(timezone.utc).isoformat() if expires_at is not None else None
        ),
        meta=row[12],
    )


_SELECT_COLUMNS = (
    ArtifactModel.run_id,
    ArtifactModel.item_id,
    ArtifactModel.creator_id,
    ArtifactModel.kind,
    ArtifactModel.storage_backend,
    ArtifactModel.storage_key,
    ArtifactModel.content_type,
    ArtifactModel.size_bytes,
    ArtifactModel.sha256,
    ArtifactModel.source_uri,
    ArtifactModel.retention_class,
    ArtifactModel.expires_at,
    ArtifactModel.meta,
)


class PostgresArtifactRepository:
    """Mantém somente metadata; os bytes continuam no backend de storage."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def record(self, artifact: ArtifactRecord) -> ArtifactRecord:
        stmt = (
            pg_insert(ArtifactModel)
            .values(
                organization_id=self._tenant.organization_id,
                id=artifact.id,
                run_id=artifact.run_id,
                item_id=artifact.item_id,
                creator_id=artifact.creator_id,
                kind=artifact.kind,
                storage_backend=artifact.storage_backend,
                storage_key=artifact.storage_key,
                content_type=artifact.content_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
                source_uri=artifact.source_uri,
                retention_class=artifact.retention_class,
                expires_at=artifact.expires_at,
                meta=artifact.meta,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "id"],
                set_={
                    "run_id": artifact.run_id,
                    "item_id": artifact.item_id,
                    "creator_id": artifact.creator_id,
                    "kind": artifact.kind,
                    "storage_backend": artifact.storage_backend,
                    "storage_key": artifact.storage_key,
                    "content_type": artifact.content_type,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                    "source_uri": artifact.source_uri,
                    "retention_class": artifact.retention_class,
                    "expires_at": artifact.expires_at,
                    "meta": artifact.meta,
                },
            )
        )
        async with self._database.connection(self._tenant) as connection:
            await self._database.execute(connection, stmt)
        return artifact

    async def get(self, artifact_id: str) -> Optional[ArtifactRecord]:
        stmt = select(*_SELECT_COLUMNS).where(
            ArtifactModel.organization_id == self._tenant.organization_id,
            ArtifactModel.id == artifact_id,
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
        return _to_record(row) if row is not None else None

    async def by_run(self, run_id: str) -> list[ArtifactRecord]:
        stmt = (
            select(*_SELECT_COLUMNS)
            .where(
                ArtifactModel.organization_id == self._tenant.organization_id,
                ArtifactModel.run_id == run_id,
            )
            .order_by(ArtifactModel.storage_key)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            rows = await cursor.fetchall()
        return [_to_record(row) for row in rows]

    async def by_key(self, storage_key: str) -> Optional[ArtifactRecord]:
        stmt = select(*_SELECT_COLUMNS).where(
            ArtifactModel.organization_id == self._tenant.organization_id,
            ArtifactModel.storage_key == storage_key,
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
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
        stmt = (
            update(ArtifactModel)
            .where(
                ArtifactModel.organization_id == self._tenant.organization_id,
                ArtifactModel.storage_key == storage_key,
            )
            .values(
                retention_class=retention_class,
                expires_at=expires_at,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            await self._database.execute(connection, stmt)

    async def set_storage_backend(
        self,
        storage_key: str,
        storage_backend: str,
    ) -> None:
        """Troca somente a localização após uma cópia já verificada."""
        stmt = (
            update(ArtifactModel)
            .where(
                ArtifactModel.organization_id == self._tenant.organization_id,
                ArtifactModel.storage_key == storage_key,
            )
            .values(storage_backend=storage_backend)
        )
        async with self._database.connection(self._tenant) as connection:
            await self._database.execute(connection, stmt)

    async def expired(self, *, now: datetime) -> list[ArtifactRecord]:
        stmt = (
            select(*_SELECT_COLUMNS)
            .where(
                ArtifactModel.organization_id == self._tenant.organization_id,
                ArtifactModel.expires_at.is_not(None),
                ArtifactModel.expires_at <= now,
            )
            .order_by(ArtifactModel.storage_key)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            rows = await cursor.fetchall()
        return [_to_record(row) for row in rows]

    async def delete(self, artifact_id: str) -> None:
        stmt = delete(ArtifactModel).where(
            ArtifactModel.organization_id == self._tenant.organization_id,
            ArtifactModel.id == artifact_id,
        )
        async with self._database.connection(self._tenant) as connection:
            await self._database.execute(connection, stmt)
