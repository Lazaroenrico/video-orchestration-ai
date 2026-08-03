"""Integração real do repositório PostgreSQL de feedback (ADR-D36, Fase 2)."""
from __future__ import annotations

from orchestrator import runner
from orchestrator.db import (
    Database,
    PostgresCreatorRepository,
    PostgresFeedbackRepository,
    TenantIdentity,
    upgrade_database,
)

SUMMARY = {
    "produced": 10,
    "approved": 8,
    "dropped": 2,
    "total_attempts": 12,
    "total_cost_usd": 1.23,
    "winning_styles": ["problem", "curiosity"],
}


def _admin_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE feedback_app LOGIN PASSWORD 'feedback_app';
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE feedback_app LOGIN PASSWORD 'feedback_app';
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO feedback_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO feedback_app"
    )
    postgresql.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO feedback_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://feedback_app:feedback_app@{info.host}:{info.port}/{info.dbname}"


def _configure_runtime(monkeypatch, runtime_url: str, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_ARTIFACTS_DB", str(tmp_path / "artifacts.sqlite"))
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_VIDEOS", str(tmp_path / "videos"))


async def test_saved_feedback_survives_repository_restart(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresFeedbackRepository(database, tenant)
        await repository.save_feedback("run-1", SUMMARY)

    async with Database(runtime_url) as restarted_database:
        tenant = await restarted_database.ensure_tenant(identity)
        loaded = await PostgresFeedbackRepository(
            restarted_database, tenant
        ).load_feedback("run-1")

    assert loaded == SUMMARY


async def test_resaved_run_is_updated_and_becomes_latest(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresFeedbackRepository(database, tenant)
        await repository.save_feedback("run-1", {**SUMMARY, "produced": 1})
        await repository.save_feedback("run-2", {**SUMMARY, "produced": 2})
        await repository.save_feedback("run-1", {**SUMMARY, "produced": 99})

        updated = await repository.load_feedback("run-1")
        latest = await repository.load_latest_feedback()

    assert updated is not None and updated["produced"] == 99
    assert latest is not None and latest["produced"] == 99


async def test_feedback_is_isolated_between_organizations(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        acme = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        globex = await database.ensure_tenant(
            TenantIdentity("globex", "Globex", "oidc|bob")
        )
        acme_repository = PostgresFeedbackRepository(database, acme)
        globex_repository = PostgresFeedbackRepository(database, globex)
        await acme_repository.save_feedback(
            "run-acme", {**SUMMARY, "winning_styles": ["acme-secret"]}
        )
        await globex_repository.save_feedback(
            "run-globex", {**SUMMARY, "winning_styles": ["globex-private"]}
        )

        assert await globex_repository.load_feedback("run-acme") is None
        assert await globex_repository.load_latest_feedback() == {
            **SUMMARY,
            "winning_styles": ["globex-private"],
        }

        async with database.connection(globex) as connection:
            cursor = await connection.execute(
                "SELECT run_id FROM run_feedback ORDER BY run_id"
            )
            raw_visible_rows = await cursor.fetchall()
            cross_tenant_update = await connection.execute(
                """
                UPDATE run_feedback
                SET summary = '{"compromised": true}'::jsonb
                WHERE run_id = 'run-acme'
                """
            )

        acme_feedback = await acme_repository.load_feedback("run-acme")

    assert raw_visible_rows == [("run-globex",)]
    assert cross_tenant_update.rowcount == 0
    assert acme_feedback is not None
    assert acme_feedback["winning_styles"] == ["acme-secret"]


async def test_run_pipeline_selects_postgres_without_creating_json(
    monkeypatch,
    pipeline_cfg,
    postgresql,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    trap = tmp_path / "must-not-be-used.json"
    _configure_runtime(monkeypatch, runtime_url, tmp_path)

    _, output = await runner.run_pipeline(
        pipeline_cfg,
        {"adapters": {"video": "mock"}},
        db_path=tmp_path / "checkpoint.sqlite",
        run_id="feedback-run",
        batch=4,
        offer="serum X",
        feedback_store=trap,
    )

    assert not trap.exists()
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        persisted = await PostgresFeedbackRepository(
            database, tenant
        ).load_feedback("feedback-run")

    assert persisted == output["feedback"]


async def test_second_pipeline_run_reads_prior_feedback_from_postgres(
    monkeypatch,
    pipeline_cfg,
    postgresql,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    trap = tmp_path / "must-not-be-used.json"
    _configure_runtime(monkeypatch, runtime_url, tmp_path)
    providers = {"adapters": {"video": "mock"}}

    _, first = await runner.run_pipeline(
        pipeline_cfg,
        providers,
        db_path=tmp_path / "checkpoint.sqlite",
        run_id="cycle-1",
        batch=6,
        feedback_store=trap,
    )
    _, second = await runner.run_pipeline(
        pipeline_cfg,
        providers,
        db_path=tmp_path / "checkpoint.sqlite",
        run_id="cycle-2",
        batch=6,
        feedback_store=trap,
    )

    winners = first["feedback"]["winning_styles"]
    assert winners
    assert second["config"]["prior_winning_styles"] == winners
    assert not trap.exists()


async def test_migration_upgrades_existing_creators_from_revision_0003(postgresql):
    admin_url = _admin_url(postgresql)
    upgrade_database(admin_url, "20260719_0003")
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("existing", "Existing", "oidc|existing")

    async with Database(runtime_url) as database:
        before_upgrade = await database.ensure_tenant(identity)
        creator_repository = PostgresCreatorRepository(database, before_upgrade)
        await creator_repository.record_creators(
            "run-existing",
            [{"id": "creator-0", "voice_ref": "voice-existing"}],
            approved_ids=["creator-0"],
        )

    upgrade_database(admin_url)
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        after_upgrade = await database.ensure_tenant(identity)
        creators = await PostgresCreatorRepository(
            database, after_upgrade
        ).load_creators()
        feedback_repository = PostgresFeedbackRepository(database, after_upgrade)
        await feedback_repository.save_feedback("run-new", SUMMARY)
        feedback = await feedback_repository.load_latest_feedback()

    assert after_upgrade == before_upgrade
    assert creators[0]["run_id"] == "run-existing"
    assert feedback == SUMMARY
