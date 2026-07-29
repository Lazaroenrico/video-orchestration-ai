"""Integração real do read model PostgreSQL de runs (ADR-D36, Fase 2)."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import orchestrator.run_store as run_store
from orchestrator.web import server as web_server
from orchestrator.db import (
    Database,
    PostgresArtifactRepository,
    PostgresCreatorRepository,
    PostgresJobRepository,
    PostgresRunRepository,
    TenantIdentity,
    upgrade_database,
)
from orchestrator.storage.db import ArtifactRecord
from orchestrator.worker import run_worker_once


def _admin_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE run_app LOGIN PASSWORD 'run_app';
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE run_app LOGIN PASSWORD 'run_app';
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO run_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO run_app"
    )
    postgresql.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO run_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://run_app:run_app@{info.host}:{info.port}/{info.dbname}"


async def test_run_repository_selector_keeps_local_mode_without_database_url(
    monkeypatch,
):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async with run_store.open_repository() as repository:
        assert repository is None


async def test_run_repository_selector_uses_postgres_when_configured(
    postgresql,
    monkeypatch,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")

    async with run_store.open_repository() as repository:
        assert isinstance(repository, PostgresRunRepository)
        await repository.start("run-selected", offer="serum X")

    async with run_store.open_repository() as restarted:
        assert restarted is not None
        persisted = await restarted.get("run-selected")

    assert persisted is not None
    assert persisted.offer == "serum X"


async def test_run_and_items_survive_repository_restart(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    item = {
        "id": "concept-0001",
        "concept": {"hook": "Stop scrolling"},
        "script": "Try serum X",
        "attempts": 1,
        "cost_usd": 0.12,
        "dropped": False,
    }
    summary = {
        "run_id": "run-1",
        "produced": 1,
        "approved": 1,
        "dropped": 0,
        "in_flight": 0,
        "total_attempts": 1,
        "total_cost_usd": 0.12,
        "cost_by_tier": {"ltx": 0.12},
        "winning_styles": ["testimonial"],
    }

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresRunRepository(database, tenant)
        await repository.start(
            "run-1",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
        )
        await repository.save(
            "run-1",
            phase="done",
            state={"run_id": "run-1", "feedback": {"winning_styles": ["testimonial"]}},
            summary=summary,
            items=[item],
        )

    async with Database(runtime_url) as restarted_database:
        tenant = await restarted_database.ensure_tenant(identity)
        persisted = await PostgresRunRepository(restarted_database, tenant).get("run-1")

    assert persisted is not None
    assert persisted.run_id == "run-1"
    assert persisted.offer == "serum X"
    assert persisted.platform == "tiktok"
    assert persisted.batch_size == 1
    assert persisted.phase == "done"
    assert persisted.summary == summary
    assert persisted.state == {
        "run_id": "run-1",
        "feedback": {"winning_styles": ["testimonial"]},
    }
    assert persisted.items == [item]


async def test_saving_run_replaces_removed_items(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    first = {"id": "concept-0001", "script": "First"}
    removed = {"id": "concept-0002", "script": "Removed"}

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresRunRepository(database, tenant)
        await repository.save(
            "run-exact",
            phase="running",
            state={"run_id": "run-exact"},
            summary={"run_id": "run-exact", "produced": 2},
            items=[first, removed],
        )
        await repository.save(
            "run-exact",
            phase="done",
            state={"run_id": "run-exact"},
            summary={"run_id": "run-exact", "produced": 1},
            items=[first],
        )
        persisted = await repository.get("run-exact")

    assert persisted is not None
    assert persisted.items == [first]


async def test_run_index_is_newest_first_and_surfaces_errors(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresRunRepository(database, tenant)
        await repository.start("run-old", offer="old")
        await repository.start("run-new", offer="new")
        await repository.save(
            "run-new",
            phase="error",
            state={"run_id": "run-new"},
            summary={"run_id": "run-new"},
            items=[],
            error="provider failed",
        )
        index = await repository.list_index()

    assert [entry.run_id for entry in index] == ["run-new", "run-old"]
    assert index[0].phase == "error"
    assert index[0].error == "provider failed"
    assert index[1].phase == "running"
    assert index[1].error is None


async def test_run_repository_rejects_unknown_phase_clearly(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresRunRepository(database, tenant)
        with pytest.raises(ValueError, match="unknown run phase 'lost'"):
            await repository.save(
                "run-invalid",
                phase="lost",
                state={},
                summary={},
                items=[],
            )


async def test_run_repository_rejects_item_without_id_clearly(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresRunRepository(database, tenant)
        with pytest.raises(ValueError, match="run item must have a non-empty id"):
            await repository.save(
                "run-invalid-item",
                phase="running",
                state={},
                summary={},
                items=[{"script": "missing id"}],
            )


async def test_completed_run_remains_available_over_http_after_restart(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_VIDEOS", str(tmp_path / "videos"))
    checkpoint = tmp_path / "checkpoint.sqlite"
    transport = httpx.ASGITransport(app=web_server.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/run",
            json={
                "offer": "serum X",
                "batch": 1,
                "platform": "tiktok",
                "config_dir": "config-mock",
                "db": str(checkpoint),
                "approve_creators": False,
                "edit_concepts": False,
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        await run_worker_once(worker_id="runner-read-model")

        web_server._runs.clear()
        assert not checkpoint.exists()

        state_response = await client.get(
            f"/api/state/{run_id}",
            params={"config_dir": "config-mock", "db": str(checkpoint)},
        )
        status_response = await client.get(
            f"/api/status/{run_id}",
            params={"config_dir": "config-mock", "db": str(checkpoint)},
        )
        index_response = await client.get(
            "/api/runs",
            params={"db": str(checkpoint)},
        )

    assert state_response.status_code == 200
    state = state_response.json()
    assert state["phase"] == "done"
    assert len(state["items"]) == 1
    assert state["summary"]["approved"] == 1
    assert status_response.status_code == 200
    assert status_response.json() == state["summary"]
    assert run_id in index_response.json()["runs"]


async def test_run_status_falls_back_to_read_model_without_checkpoint(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    summary = {
        "run_id": "run-read-model-only",
        "produced": 1,
        "approved": 1,
        "dropped": 0,
        "in_flight": 0,
    }
    async with run_store.open_repository() as repository:
        assert repository is not None
        await repository.save(
            "run-read-model-only",
            phase="done",
            state={"run_id": "run-read-model-only"},
            summary=summary,
            items=[],
        )

    trap = tmp_path / "missing.sqlite"
    transport = httpx.ASGITransport(app=web_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/status/run-read-model-only",
            params={"config_dir": "config-mock", "db": str(trap)},
        )

    assert response.status_code == 200
    assert response.json() == summary
    assert not trap.exists()


async def test_failed_run_remains_available_over_http_after_restart(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    checkpoint = tmp_path / "failed-checkpoint.sqlite"
    transport = httpx.ASGITransport(app=web_server.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/run",
            json={
                "offer": "serum X",
                "batch": 1,
                "config_dir": str(tmp_path / "missing-config"),
                "db": str(checkpoint),
                "approve_creators": False,
                "edit_concepts": False,
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        base = datetime.now(UTC) + timedelta(seconds=1)
        for offset in (0, 5, 15, 35, 75):
            await run_worker_once(
                worker_id="runner-failure",
                now=base + timedelta(seconds=offset),
            )
        web_server._runs.clear()

        state_response = await client.get(
            f"/api/state/{run_id}",
            params={
                "config_dir": "config-mock",
                "db": str(checkpoint),
            },
        )
        index_response = await client.get(
            "/api/runs",
            params={"db": str(checkpoint)},
        )

    assert state_response.status_code == 200
    state = state_response.json()
    assert state["phase"] == "error"
    assert "pipeline.yaml" in state["error"]
    assert run_id in index_response.json()["errored"]


async def test_review_gate_is_persisted_while_waiting_for_decision(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_VIDEOS", str(tmp_path / "videos"))
    response = await web_server.start_run_v2(
        web_server.RunV2Request(
            campaign={
                "offer": "serum X",
                "audience": "Adults with dry skin",
                "batch_size": 1,
                "platform": "tiktok",
            },
            config_dir="config-mock",
            db=str(tmp_path / "checkpoint.sqlite"),
        ),
        web_server.BackgroundTasks(),
    )
    run_id = response["run_id"]
    await run_worker_once(worker_id="runner-before-edit")

    async with run_store.open_repository() as repository:
        assert repository is not None
        persisted = await repository.get(run_id)
    async with web_server.job_store.open_repository() as jobs:
        assert jobs is not None
        gate = await jobs.get_pending_gate(run_id)

    assert persisted is not None
    assert persisted.phase == "review"
    assert gate is not None
    assert gate.gate_type == "review_creative_plan"
    pending = gate.payload["concepts"]
    assert persisted.state["concepts"] == pending
    assert len(gate.payload["creators"]) == 2

    await web_server.review_run_v2(
        run_id,
        web_server.ReviewV2Request(
            action="approve",
            gate_id=str(gate.gate_id),
            version=gate.version,
            gate_type="review_creative_plan",
        ),
    )
    await run_worker_once(worker_id="runner-after-edit")


async def test_review_gate_persists_creators_until_approval(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_VIDEOS", str(tmp_path / "videos"))
    response = await web_server.start_run_v2(
        web_server.RunV2Request(
            campaign={
                "offer": "serum X",
                "audience": "Adults with dry skin",
                "batch_size": 1,
                "platform": "tiktok",
            },
            config_dir="config-mock",
            db=str(tmp_path / "checkpoint.sqlite"),
        ),
        web_server.BackgroundTasks(),
    )
    run_id = response["run_id"]
    await run_worker_once(worker_id="runner-before-approval")

    async with run_store.open_repository() as repository:
        assert repository is not None
        persisted = await repository.get(run_id)
    async with web_server.job_store.open_repository() as jobs:
        assert jobs is not None
        gate = await jobs.get_pending_gate(run_id)

    assert persisted is not None
    assert persisted.phase == "review"
    assert gate is not None
    assert gate.gate_type == "review_creative_plan"
    pending = gate.payload["creators"]
    assert pending and all(creator["id"] for creator in pending)

    await web_server.review_run_v2(
        run_id,
        web_server.ReviewV2Request(
            action="approve",
            gate_id=str(gate.gate_id),
            version=gate.version,
            gate_type="review_creative_plan",
        ),
    )
    await run_worker_once(worker_id="runner-after-approval")
    async with Database(_runtime_url(postgresql)) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        creators = await PostgresCreatorRepository(database, tenant).load_creators()

    assert {creator["status"] for creator in creators} == {"approved"}


async def test_item_progress_is_persisted_before_stream_delivery(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_VIDEOS", str(tmp_path / "videos"))
    run_id = "run-progress"
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}
    async with run_store.open_repository() as repository:
        assert repository is not None
        await repository.start(run_id, offer="serum X", batch_size=1)

    reached_item_update = asyncio.Event()
    never_release = asyncio.Event()
    original_emit = web_server._emit

    async def pause_before_item_delivery(event_run_id, event):
        if event.get("type") == "item_update":
            reached_item_update.set()
            await never_release.wait()
        await original_emit(event_run_id, event)

    monkeypatch.setattr(web_server, "_emit", pause_before_item_delivery)
    task = asyncio.create_task(
        web_server._execute_run(
            run_id,
            offer="serum X",
            batch=1,
            platform="tiktok",
            config_dir="config-mock",
            db_path=str(tmp_path / "checkpoint.sqlite"),
            approve_creators=False,
            edit_concepts=False,
        )
    )
    await asyncio.wait_for(reached_item_update.wait(), timeout=5)

    async with run_store.open_repository() as repository:
        assert repository is not None
        persisted = await repository.get(run_id)

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert persisted is not None
    assert persisted.phase == "running"
    assert len(persisted.items) == 1
    assert persisted.items[0]["id"]


async def test_run_repository_is_isolated_by_organization(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    acme_identity = TenantIdentity("acme", "Acme", "oidc|alice")
    globex_identity = TenantIdentity("globex", "Globex", "oidc|bob")

    async with Database(runtime_url) as database:
        acme = await database.ensure_tenant(acme_identity)
        globex = await database.ensure_tenant(globex_identity)
        acme_runs = PostgresRunRepository(database, acme)
        globex_runs = PostgresRunRepository(database, globex)
        await acme_runs.save(
            "shared-id",
            phase="done",
            state={"tenant": "acme"},
            summary={"run_id": "shared-id", "approved": 1},
            items=[{"id": "acme-item"}],
        )

        assert await globex_runs.get("shared-id") is None
        assert await globex_runs.list_index() == []

        await globex_runs.save(
            "shared-id",
            phase="error",
            state={"tenant": "globex"},
            summary={"run_id": "shared-id", "approved": 0},
            items=[{"id": "globex-item"}],
            error="globex-only",
        )
        acme_snapshot = await acme_runs.get("shared-id")
        globex_snapshot = await globex_runs.get("shared-id")

    assert acme_snapshot is not None
    assert acme_snapshot.state == {"tenant": "acme"}
    assert acme_snapshot.items == [{"id": "acme-item"}]
    assert globex_snapshot is not None
    assert globex_snapshot.state == {"tenant": "globex"}
    assert globex_snapshot.items == [{"id": "globex-item"}]


async def test_start_resets_an_existing_run_projection(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresRunRepository(database, tenant)
        await repository.save(
            "run-restarted",
            phase="error",
            state={"stale": True},
            summary={"run_id": "run-restarted", "approved": 1},
            items=[{"id": "stale-item"}],
            error="old failure",
        )
        await repository.start(
            "run-restarted",
            offer="new offer",
            platform="instagram",
            batch_size=2,
        )
        restarted = await repository.get("run-restarted")

    assert restarted is not None
    assert restarted.phase == "running"
    assert restarted.error is None
    assert restarted.offer == "new offer"
    assert restarted.platform == "instagram"
    assert restarted.batch_size == 2
    assert restarted.summary == {}
    assert restarted.state == {}
    assert restarted.items == []


async def test_migration_upgrades_artifacts_from_revision_0005(postgresql):
    admin_url = _admin_url(postgresql)
    upgrade_database(admin_url, "20260719_0005")
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("existing", "Existing", "oidc|existing")
    artifact = ArtifactRecord(
        run_id="run-existing",
        kind="video",
        storage_backend="r2",
        storage_key="run-existing/items/item-0/assembled.mp4",
    )

    async with Database(runtime_url) as database:
        tenant_before = await database.ensure_tenant(identity)
        await PostgresArtifactRepository(database, tenant_before).record(artifact)

    upgrade_database(admin_url)
    runtime_url = _runtime_url(postgresql)
    async with Database(runtime_url) as database:
        tenant_after = await database.ensure_tenant(identity)
        loaded_artifact = await PostgresArtifactRepository(
            database,
            tenant_after,
        ).by_key(artifact.storage_key)
        runs = PostgresRunRepository(database, tenant_after)
        await runs.start("run-new", offer="serum X")
        loaded_run = await runs.get("run-new")

    assert tenant_after == tenant_before
    assert loaded_artifact == artifact
    assert loaded_run is not None
    assert loaded_run.offer == "serum X"


async def test_run_state_signs_r2_uris_without_mutating_postgres(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    pointer = "r2://ugc/run-r2/items/item-0/assembled.mp4"
    item = {
        "id": "item-0",
        "concept": {},
        "attempts": 1,
        "cost_usd": 0.12,
        "artifacts": [{"kind": "video", "uri": pointer}],
        "assembled": {"kind": "video", "uri": pointer},
        "dropped": False,
    }

    async with run_store.open_repository() as repository:
        assert repository is not None
        await repository.save(
            "run-r2",
            phase="done",
            state={"run_id": "run-r2"},
            summary={"run_id": "run-r2", "approved": 1},
            items=[item],
        )

    class SigningStorage:
        backend = "r2"

        async def get_signed_url(self, key, ttl_seconds=900):
            return f"https://signed.example/{key}?ttl={ttl_seconds}"

    monkeypatch.setattr(
        web_server,
        "_signing_storage",
        lambda _config_dir: SigningStorage(),
    )
    transport = httpx.ASGITransport(app=web_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/state/run-r2",
            params={"config_dir": "config-mock", "db": str(tmp_path / "missing.sqlite")},
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["assembled"]["uri"].startswith(
        "https://signed.example/"
    )
    async with run_store.open_repository() as repository:
        assert repository is not None
        persisted = await repository.get("run-r2")

    assert persisted is not None
    assert persisted.items[0]["assembled"]["uri"] == pointer


async def test_run_state_restores_persisted_concept_gate_after_restart(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    concepts = [{"id": "concept-1", "script": "Edit me"}]
    async with run_store.open_repository() as repository:
        assert repository is not None
        await repository.save(
            "run-edit-restart",
            phase="editing",
            state={"run_id": "run-edit-restart", "pending_concepts": concepts},
            summary={"run_id": "run-edit-restart", "in_flight": 0},
            items=[],
        )

    web_server._runs.pop("run-edit-restart", None)
    transport = httpx.ASGITransport(app=web_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/state/run-edit-restart",
            params={"config_dir": "config-mock", "db": str(tmp_path / "missing.sqlite")},
        )

    assert response.status_code == 200
    assert response.json()["phase"] == "editing"
    assert response.json()["edit_concepts"] == concepts


async def test_run_state_restores_persisted_creator_gate_after_restart(
    postgresql,
    monkeypatch,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    creators = [
        {
            "creator_id": "creator-1",
            "image": "r2://ugc/creators/creator-1.png",
            "voice": "voice-1",
            "angles": ["front"],
        }
    ]
    expected = [
        {
            "id": "creator-1",
            "image_uri": "r2://ugc/creators/creator-1.png",
            "voice_ref": "voice-1",
            "voice_preview_uri": None,
            "image": "r2://ugc/creators/creator-1.png",
            "voice": "voice-1",
            "angles": ["front"],
        }
    ]
    async with run_store.open_repository() as repository:
        assert repository is not None
        await repository.save(
            "run-awaiting-restart",
            phase="awaiting",
            state={
                "run_id": "run-awaiting-restart",
                "pending_creators": creators,
            },
            summary={"run_id": "run-awaiting-restart", "in_flight": 0},
            items=[],
        )

    web_server._runs.pop("run-awaiting-restart", None)
    transport = httpx.ASGITransport(app=web_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/state/run-awaiting-restart",
            params={"config_dir": "config-mock", "db": str(tmp_path / "missing.sqlite")},
        )

    assert response.status_code == 200
    assert response.json()["phase"] == "awaiting"
    assert response.json()["awaiting"] == expected
