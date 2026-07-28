"""Integração real do repositório PostgreSQL de artifacts (ADR-D36, Fase 2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator import runner
from orchestrator.db import (
    Database,
    PostgresArtifactRepository,
    PostgresFeedbackRepository,
    TenantIdentity,
    upgrade_database,
)
from orchestrator.storage.db import ArtifactRecord, open_artifact_repository
from orchestrator.storage.retention import RETENTION_REJECTED, purge_expired


_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _admin_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE artifact_app LOGIN PASSWORD 'artifact_app';
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE artifact_app LOGIN PASSWORD 'artifact_app';
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO artifact_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO artifact_app"
    )
    postgresql.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO artifact_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://artifact_app:artifact_app@{info.host}:{info.port}/{info.dbname}"


async def test_r2_artifact_metadata_survives_repository_restart(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    artifact = ArtifactRecord(
        run_id="run-1",
        item_id="item-0",
        kind="clip",
        storage_backend="r2",
        storage_key="run-1/items/item-0/clip-0.mp4",
        content_type="video/mp4",
        size_bytes=1024,
        sha256="a" * 64,
        source_uri="https://replicate.delivery/source.mp4",
        meta={"tier": "ltx", "take": 1},
    )

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        await PostgresArtifactRepository(database, tenant).record(artifact)

    async with Database(runtime_url) as restarted_database:
        tenant = await restarted_database.ensure_tenant(identity)
        loaded = await PostgresArtifactRepository(restarted_database, tenant).get(artifact.id)

    assert loaded == artifact


async def test_recording_same_pointer_upserts_without_duplication(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresArtifactRepository(database, tenant)
        first = ArtifactRecord(
            run_id="run-1",
            kind="clip",
            storage_backend="r2",
            storage_key="run-1/items/item-0/clip-0.mp4",
            size_bytes=10,
        )
        await repository.record(first)
        await repository.record(
            ArtifactRecord(
                run_id="run-1",
                kind="clip",
                storage_backend="r2",
                storage_key=first.storage_key,
                size_bytes=999,
            )
        )

        rows = await repository.by_run("run-1")

    assert len(rows) == 1
    assert rows[0].size_bytes == 999


async def test_retention_reclassifies_artifact_by_canonical_key(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresArtifactRepository(database, tenant)
        artifact = ArtifactRecord(
            run_id="run-1",
            kind="clip",
            storage_backend="r2",
            storage_key="run-1/items/item-0/clip-0.mp4",
        )
        await repository.record(artifact)

        await repository.set_retention(
            artifact.storage_key,
            RETENTION_REJECTED,
            now=_NOW,
        )
        loaded = await repository.by_key(artifact.storage_key)

    assert loaded is not None
    assert loaded.retention_class == RETENTION_REJECTED
    assert loaded.expires_at == (_NOW + timedelta(days=3)).isoformat()


async def test_purge_uses_postgres_metadata_and_removes_the_row(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    expired = ArtifactRecord(
        run_id="run-1",
        kind="clip",
        storage_backend="r2",
        storage_key="run-1/items/item-0/clip-0.mp4",
        retention_class=RETENTION_REJECTED,
        expires_at=(_NOW - timedelta(days=1)).isoformat(),
    )

    class TrackingStorage:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, storage_key: str) -> None:
            self.deleted.append(storage_key)

    storage = TrackingStorage()
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresArtifactRepository(database, tenant)
        await repository.record(expired)

        purged = await purge_expired(repository, storage, now=_NOW)
        loaded = await repository.by_key(expired.storage_key)

    assert purged == [expired.storage_key]
    assert storage.deleted == [expired.storage_key]
    assert loaded is None


async def test_artifacts_are_isolated_between_organizations(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        acme = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        globex = await database.ensure_tenant(
            TenantIdentity("globex", "Globex", "oidc|bob")
        )
        acme_repository = PostgresArtifactRepository(database, acme)
        globex_repository = PostgresArtifactRepository(database, globex)
        await acme_repository.record(
            ArtifactRecord(
                run_id="run-acme",
                kind="clip",
                storage_backend="r2",
                storage_key="run-acme/private.mp4",
                source_uri="https://source.example/acme",
            )
        )
        await globex_repository.record(
            ArtifactRecord(
                run_id="run-globex",
                kind="clip",
                storage_backend="r2",
                storage_key="run-globex/private.mp4",
            )
        )

        async with database.connection(globex) as connection:
            cursor = await connection.execute(
                "SELECT run_id FROM artifacts ORDER BY run_id"
            )
            raw_visible_rows = await cursor.fetchall()
            cross_tenant_update = await connection.execute(
                """
                UPDATE artifacts
                SET source_uri = 'https://attacker.example/stolen'
                WHERE storage_key = 'run-acme/private.mp4'
                """
            )

        acme_artifact = await acme_repository.by_key("run-acme/private.mp4")

    assert raw_visible_rows == [("run-globex",)]
    assert cross_tenant_update.rowcount == 0
    assert acme_artifact is not None
    assert acme_artifact.source_uri == "https://source.example/acme"


async def test_open_repository_selects_postgres_without_creating_sqlite(
    monkeypatch,
    postgresql,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    sqlite_trap = tmp_path / "must-not-be-created.sqlite"
    artifact = ArtifactRecord(
        run_id="run-1",
        kind="video",
        storage_backend="r2",
        storage_key="run-1/items/item-0/assembled.mp4",
    )

    async with open_artifact_repository(sqlite_trap) as repository:
        await repository.record(artifact)

    assert not sqlite_trap.exists()
    async with open_artifact_repository(sqlite_trap) as restarted_repository:
        loaded = await restarted_repository.by_key(artifact.storage_key)

    assert loaded == artifact


async def test_run_pipeline_keeps_postgres_repository_alive_for_graph(
    monkeypatch,
    pipeline_cfg,
    postgresql,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    sqlite_trap = tmp_path / "must-not-be-created.sqlite"
    monkeypatch.setenv("ORCH_ARTIFACTS_DB", str(sqlite_trap))
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_VIDEOS", str(tmp_path / "videos"))
    artifact = ArtifactRecord(
        run_id="run-graph",
        kind="video",
        storage_backend="r2",
        storage_key="run-graph/items/item-0/assembled.mp4",
    )

    class FakeGraph:
        async def ainvoke(self, initial_state, config):
            await config["configurable"]["artifact_db"].record(artifact)
            return {"run_id": initial_state["run_id"], "results": []}

    monkeypatch.setattr(runner, "build_graph", lambda *_args, **_kwargs: FakeGraph())

    await runner.run_pipeline(
        pipeline_cfg,
        {"adapters": {"video": "mock"}},
        db_path=tmp_path / "checkpoint.sqlite",
        run_id="run-graph",
    )

    assert not sqlite_trap.exists()
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        loaded = await PostgresArtifactRepository(database, tenant).by_key(
            artifact.storage_key
        )

    assert loaded == artifact


async def test_migration_upgrades_existing_feedback_from_revision_0004(postgresql):
    admin_url = _admin_url(postgresql)
    upgrade_database(admin_url, "20260719_0004")
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("existing", "Existing", "oidc|existing")
    summary = {"winning_styles": ["problem"], "approved": 3}

    async with Database(runtime_url) as database:
        tenant_before = await database.ensure_tenant(identity)
        await PostgresFeedbackRepository(database, tenant_before).save_feedback(
            "run-existing",
            summary,
        )

    upgrade_database(admin_url)
    runtime_url = _runtime_url(postgresql)
    artifact = ArtifactRecord(
        run_id="run-new",
        kind="video",
        storage_backend="r2",
        storage_key="run-new/items/item-0/assembled.mp4",
    )

    async with Database(runtime_url) as database:
        tenant_after = await database.ensure_tenant(identity)
        feedback = await PostgresFeedbackRepository(
            database,
            tenant_after,
        ).load_feedback("run-existing")
        repository = PostgresArtifactRepository(database, tenant_after)
        await repository.record(artifact)
        loaded = await repository.by_key(artifact.storage_key)

    assert tenant_after == tenant_before
    assert feedback == summary
    assert loaded == artifact
