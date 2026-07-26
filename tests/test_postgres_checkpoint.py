"""Integração real do checkpointer PostgreSQL (ADR-D36, Fase 2)."""
from __future__ import annotations

from orchestrator.db import Database, TenantIdentity, upgrade_database
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator import runner


_PROVIDERS = {"adapters": {"video": "mock"}}


def _admin_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE checkpoint_app LOGIN;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE, CREATE ON SCHEMA public TO checkpoint_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        "TO checkpoint_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://checkpoint_app@{info.host}:{info.port}/{info.dbname}"


async def test_postgres_checkpoint_survives_restart_without_local_file(
    postgresql,
    monkeypatch,
    tmp_path,
    run_config,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    pipeline = run_config["configurable"]["pipeline"]
    thread = {
        "configurable": dict(
            run_config["configurable"],
            thread_id="run-postgres-checkpoint",
        ),
        "max_concurrency": 4,
        "recursion_limit": 50,
    }
    init = {
        "run_id": "run-postgres-checkpoint",
        "config": {"offer": "serum", "batch_size": 2},
    }
    first_trap = tmp_path / "first.sqlite"
    second_trap = tmp_path / "second.sqlite"

    async with open_checkpointer(first_trap) as checkpointer:
        app = build_graph(pipeline, checkpointer=checkpointer)
        await app.ainvoke(init, thread)

    async with open_checkpointer(second_trap) as restarted:
        app = build_graph(pipeline, checkpointer=restarted)
        snapshot = await app.aget_state(thread)

    assert snapshot.values["run_id"] == "run-postgres-checkpoint"
    assert len(snapshot.values["results"]) == 2
    assert not first_trap.exists()
    assert not second_trap.exists()


async def test_postgres_checkpoint_isolated_by_organization(
    postgresql,
    monkeypatch,
    tmp_path,
    run_config,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    pipeline = run_config["configurable"]["pipeline"]
    thread = {
        "configurable": dict(run_config["configurable"], thread_id="shared-run"),
        "max_concurrency": 4,
        "recursion_limit": 50,
    }

    async with open_checkpointer(tmp_path / "trap.sqlite") as checkpointer:
        app = build_graph(pipeline, checkpointer=checkpointer)
        await app.ainvoke(
            {"run_id": "shared-run", "config": {"batch_size": 1}},
            thread,
        )

    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "globex")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Globex")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|bob")
    async with open_checkpointer(tmp_path / "other-trap.sqlite") as checkpointer:
        app = build_graph(pipeline, checkpointer=checkpointer)
        globex_snapshot = await app.aget_state(thread)

    assert not globex_snapshot.values


async def test_postgres_checkpoint_rls_blocks_cross_organization_sql(
    postgresql,
    monkeypatch,
    tmp_path,
    run_config,
):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    pipeline = run_config["configurable"]["pipeline"]
    thread = {
        "configurable": dict(run_config["configurable"], thread_id="run-private"),
        "max_concurrency": 4,
        "recursion_limit": 50,
    }

    async with open_checkpointer(tmp_path / "trap.sqlite") as checkpointer:
        app = build_graph(pipeline, checkpointer=checkpointer)
        await app.ainvoke(
            {"run_id": "run-private", "config": {"batch_size": 1}},
            thread,
        )

    globex = TenantIdentity("globex", "Globex", "oidc|bob").context()
    async with Database(runtime_url) as database:
        async with database.connection(globex) as connection:
            cursor = await connection.execute("SELECT count(*) FROM checkpoints")
            visible = (await cursor.fetchone())[0]

    assert visible == 0


async def test_runner_run_status_and_resume_use_postgres_without_sqlite(
    postgresql,
    monkeypatch,
    tmp_path,
    pipeline_cfg,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_VIDEOS", str(tmp_path / "videos"))
    trap = tmp_path / "runner.sqlite"

    run_id, output = await runner.run_pipeline(
        pipeline_cfg,
        _PROVIDERS,
        db_path=trap,
        run_id="run-runner-postgres",
        batch=1,
    )
    status = await runner.get_status(
        pipeline_cfg,
        db_path=tmp_path / "status.sqlite",
        run_id=run_id,
    )
    resumed_id, resumed = await runner.resume_pipeline(
        pipeline_cfg,
        _PROVIDERS,
        db_path=tmp_path / "resume.sqlite",
        run_id=run_id,
    )

    assert status is not None
    assert len(status["results"]) == len(output["results"]) == 1
    assert resumed_id == run_id
    assert len(resumed["results"]) == 1
    assert not trap.exists()
    assert not (tmp_path / "status.sqlite").exists()
    assert not (tmp_path / "resume.sqlite").exists()


async def test_postgres_checkpoint_public_lifecycle_keeps_external_run_id(
    postgresql,
    monkeypatch,
    tmp_path,
    run_config,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    pipeline = run_config["configurable"]["pipeline"]
    thread = {
        "configurable": dict(run_config["configurable"], thread_id="run-lifecycle"),
        "max_concurrency": 4,
        "recursion_limit": 50,
    }

    async with open_checkpointer(tmp_path / "trap.sqlite") as checkpointer:
        app = build_graph(pipeline, checkpointer=checkpointer)
        await app.ainvoke(
            {"run_id": "run-lifecycle", "config": {"batch_size": 1}},
            thread,
        )
        checkpoints = [
            value
            async for value in checkpointer.alist(
                {"configurable": {"thread_id": "run-lifecycle"}},
                limit=1,
            )
        ]
        history = await checkpointer.aget_delta_channel_history(
            config=checkpoints[0].config,
            channels=["results"],
        )
        await checkpointer.adelete_thread("run-lifecycle")
        deleted = await app.aget_state(thread)

    assert checkpoints[0].config["configurable"]["thread_id"] == "run-lifecycle"
    assert "results" in history
    assert not deleted.values
