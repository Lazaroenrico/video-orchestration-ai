"""Operação reconstruível e tenant-scoped da ADR-D36, Fase 5."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

from click.testing import CliRunner
import pytest

from orchestrator.cli import cli
from orchestrator.db import Database, TenantIdentity, upgrade_database
from orchestrator.db.artifacts import PostgresArtifactRepository
from orchestrator.db.effects import PostgresEffectLedger
from orchestrator.db.jobs import PostgresJobRepository
from orchestrator.db.runs import PostgresRunRepository
from orchestrator.operations import OperationalThresholds, PostgresOperations
from orchestrator.storage.db import ArtifactRecord


def _database_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_database_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE tenant_app LOGIN PASSWORD 'tenant_app';
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE tenant_app LOGIN PASSWORD 'tenant_app';
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO tenant_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tenant_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://tenant_app:tenant_app@{info.host}:{info.port}/{info.dbname}"


async def test_inspect_run_reconstructs_durable_state_by_run_id(postgresql):
    upgrade_database(_database_url(postgresql))
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    async with Database(_runtime_database_url(postgresql)) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        jobs = PostgresJobRepository(database, tenant)
        runs = PostgresRunRepository(database, tenant)
        artifacts = PostgresArtifactRepository(database, tenant)
        effects = PostgresEffectLedger(database, tenant)

        await jobs.enqueue_run(
            "run-42",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={"config_dir": "config-staging"},
            now=now,
        )
        await runs.save(
            "run-42",
            phase="awaiting",
            state={"quality": "ready"},
            summary={"total_cost_usd": 1.25},
            items=[{"id": "item-1", "status": "approved"}],
        )
        await jobs.open_gate(
            "run-42",
            gate_type="approve_creators",
            payload={"creator_ids": ["creator-1"]},
            now=now,
        )
        await artifacts.record(
            ArtifactRecord(
                run_id="run-42",
                item_id="item-1",
                kind="assembled",
                storage_backend="r2",
                storage_key="acme/run-42/final.mp4",
                size_bytes=1234,
                sha256="a" * 64,
            )
        )
        await effects.set_quota("replicate", limit_units=100)
        await effects.reserve(
            "effect-1",
            run_id="run-42",
            provider="replicate",
            units=8,
            request={"model": "video"},
        )

        report = await PostgresOperations(database, tenant).inspect_run("run-42")

    assert report["run"] == {
        "run_id": "run-42",
        "phase": "awaiting",
        "offer": "serum X",
        "platform": "tiktok",
        "batch_size": 1,
        "error": None,
        "summary": {"total_cost_usd": 1.25},
        "state": {"quality": "ready"},
    }
    assert report["items"] == [{"id": "item-1", "status": "approved"}]
    assert report["jobs"][0]["kind"] == "execute_run"
    assert report["gates"][0]["gate_type"] == "approve_creators"
    assert [event["seq"] for event in report["events"]] == sorted(
        event["seq"] for event in report["events"]
    )
    assert report["artifacts"][0]["storage_key"] == "acme/run-42/final.mp4"
    assert report["effects"][0]["provider"] == "replicate"
    assert report["metrics"]["artifact_bytes"] == 1234
    assert report["metrics"]["total_cost_usd"] == 1.25
    assert report["metrics"]["provider_units"] == {"replicate": 8}


async def test_health_snapshot_detects_every_actionable_operational_alert(postgresql):
    upgrade_database(_database_url(postgresql))
    started_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    async with Database(_runtime_database_url(postgresql)) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        jobs = PostgresJobRepository(database, tenant)
        runs = PostgresRunRepository(database, tenant)
        effects = PostgresEffectLedger(database, tenant)

        await jobs.enqueue_run(
            "run-alert",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            now=started_at,
        )
        await runs.save(
            "run-alert",
            phase="running",
            state={},
            summary={"total_cost_usd": 12.5},
            items=[],
        )
        await jobs.claim("worker-stale", now=started_at)
        await jobs.append_event(
            "run-alert",
            "storage_signing_error",
            {"key": "run-alert/final.mp4"},
            now=started_at,
        )

        delivery_time = started_at
        for attempt in range(5):
            entries = await jobs.claim_outbox("publisher", now=delivery_time)
            assert entries[0].attempt == attempt + 1
            failed = await jobs.fail_outbox(
                entries[0].entry_id,
                worker_id="publisher",
                error="queue unavailable",
                now=delivery_time,
            )
            delivery_time += timedelta(minutes=6)
        assert failed.status == "failed"

        await effects.set_quota("replicate", limit_units=10)
        await effects.reserve(
            "effect-alert",
            run_id="run-alert",
            provider="replicate",
            units=8,
            request={},
        )

        snapshot = await PostgresOperations(database, tenant).health_snapshot(
            now=started_at + timedelta(minutes=31),
            thresholds=OperationalThresholds(
                stream_lag_seconds=60,
                provider_quota_ratio=0.8,
                anomalous_cost_usd=10,
            ),
        )

    assert snapshot["metrics"]["jobs"]["running"] == 1
    assert snapshot["metrics"]["expired_job_leases"] == 1
    assert snapshot["metrics"]["outbox"]["failed"] == 1
    assert snapshot["metrics"]["provider_quotas"]["replicate"] == {
        "used_units": 8,
        "limit_units": 10,
        "ratio": 0.8,
    }
    assert {alert["code"] for alert in snapshot["alerts"]} == {
        "expired_job_lease",
        "outbox_dlq",
        "storage_signing_error",
        "stream_lag",
        "provider_limit",
        "anomalous_spend",
    }


async def test_object_inventory_reports_missing_bytes_without_changing_metadata(postgresql):
    upgrade_database(_database_url(postgresql))

    class _Storage:
        backend = "r2"

        async def exists(self, key: str) -> bool:
            return key.endswith("present.mp4")

    async with Database(_runtime_database_url(postgresql)) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        runs = PostgresRunRepository(database, tenant)
        artifacts = PostgresArtifactRepository(database, tenant)
        await runs.start("run-inventory")
        for name in ("present.mp4", "missing.mp4"):
            await artifacts.record(
                ArtifactRecord(
                    run_id="run-inventory",
                    kind="clip",
                    storage_backend="r2",
                    storage_key=f"acme/run-inventory/{name}",
                    size_bytes=10,
                    sha256=name[0] * 64,
                )
            )

        inventory = await PostgresOperations(database, tenant).object_inventory(
            _Storage()
        )
        persisted = await artifacts.by_run("run-inventory")

    assert inventory == {
        "backend": "r2",
        "object_count": 2,
        "expected_bytes": 20,
        "verified_count": 1,
        "missing": ["acme/run-inventory/missing.mp4"],
    }
    assert [artifact.storage_backend for artifact in persisted] == ["r2", "r2"]


async def test_object_inventory_checks_both_backends_during_cutover(postgresql):
    upgrade_database(_database_url(postgresql))

    class _DualStorage:
        backend = "s3"
        backends = {"r2": object(), "s3": object()}

        async def exists_in(self, backend: str, key: str) -> bool:
            return (backend, key) in {
                ("r2", "acme/old.mp4"),
                ("s3", "acme/new.mp4"),
            }

    async with Database(_runtime_database_url(postgresql)) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        artifacts = PostgresArtifactRepository(database, tenant)
        for backend, key in (("r2", "acme/old.mp4"), ("s3", "acme/new.mp4")):
            await artifacts.record(
                ArtifactRecord(
                    run_id="run-dual",
                    kind="clip",
                    storage_backend=backend,
                    storage_key=key,
                    size_bytes=10,
                )
            )

        inventory = await PostgresOperations(database, tenant).object_inventory(
            _DualStorage()
        )

    assert inventory == {
        "backend": "dual",
        "object_count": 2,
        "expected_bytes": 20,
        "verified_count": 2,
        "missing": [],
        "by_backend": {
            "r2": {"object_count": 1, "expected_bytes": 10, "verified_count": 1},
            "s3": {"object_count": 1, "expected_bytes": 10, "verified_count": 1},
        },
    }


async def test_run_inspection_is_isolated_between_organizations(postgresql):
    upgrade_database(_database_url(postgresql))

    async with Database(_runtime_database_url(postgresql)) as database:
        acme = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        globex = await database.ensure_tenant(
            TenantIdentity("globex", "Globex", "oidc|bob")
        )
        await PostgresRunRepository(database, acme).start(
            "shared-run",
            offer="Acme offer",
        )
        await PostgresRunRepository(database, globex).start(
            "shared-run",
            offer="Globex offer",
        )

        acme_report = await PostgresOperations(database, acme).inspect_run("shared-run")
        globex_report = await PostgresOperations(database, globex).inspect_run(
            "shared-run"
        )
        with pytest.raises(ValueError, match="inexistente"):
            await PostgresOperations(database, acme).inspect_run("globex-only")

    assert acme_report["run"]["offer"] == "Acme offer"
    assert globex_report["run"]["offer"] == "Globex offer"
    assert acme_report["organization_id"] != globex_report["organization_id"]


def test_ops_inspect_run_cli_outputs_portable_json(postgresql):
    upgrade_database(_database_url(postgresql))
    runtime_url = _runtime_database_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")

    async def _prepare() -> None:
        async with Database(runtime_url) as database:
            tenant = await database.ensure_tenant(identity)
            await PostgresRunRepository(database, tenant).start(
                "run-cli",
                offer="serum X",
                platform="reels",
                batch_size=2,
            )

    asyncio.run(_prepare())

    result = CliRunner().invoke(
        cli,
        ["ops", "inspect-run", "run-cli"],
        env={
            "DATABASE_URL": runtime_url,
            "ORCH_ORGANIZATION_SLUG": identity.organization_slug,
            "ORCH_ORGANIZATION_NAME": identity.organization_name,
            "ORCH_USER_SUBJECT": identity.user_subject,
        },
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run"]["run_id"] == "run-cli"
    assert payload["run"]["platform"] == "reels"
    assert payload["organization_id"]

    missing = CliRunner().invoke(
        cli,
        ["ops", "inspect-run", "missing"],
        env={
            "DATABASE_URL": runtime_url,
            "ORCH_ORGANIZATION_SLUG": identity.organization_slug,
            "ORCH_ORGANIZATION_NAME": identity.organization_name,
            "ORCH_USER_SUBJECT": identity.user_subject,
        },
    )
    assert missing.exit_code == 1
    assert "inexistente" in missing.output


def test_ops_maintain_purges_expired_bytes_and_emits_inventory(
    monkeypatch,
    postgresql,
):
    upgrade_database(_database_url(postgresql))
    runtime_url = _runtime_database_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    deleted: list[str] = []

    class _Storage:
        backend = "r2"

        async def delete(self, key: str) -> None:
            deleted.append(key)

        async def exists(self, _key: str) -> bool:
            return True

    async def _prepare() -> None:
        async with Database(runtime_url) as database:
            tenant = await database.ensure_tenant(identity)
            runs = PostgresRunRepository(database, tenant)
            artifacts = PostgresArtifactRepository(database, tenant)
            await runs.start("run-purge")
            await artifacts.record(
                ArtifactRecord(
                    run_id="run-purge",
                    kind="clip",
                    storage_backend="r2",
                    storage_key="acme/run-purge/rejected.mp4",
                )
            )
            await artifacts.set_retention(
                "acme/run-purge/rejected.mp4",
                "rejected",
                now=datetime(2020, 1, 1, tzinfo=UTC),
            )
            await runs.save(
                "run-purge",
                phase="done",
                state={},
                summary={},
                items=[],
            )

    asyncio.run(_prepare())
    monkeypatch.setattr(
        "orchestrator.cli.build_media_storage",
        lambda *_args, **_kwargs: _Storage(),
    )

    result = CliRunner().invoke(
        cli,
        ["ops", "maintain", "--config-dir", "config-staging"],
        env={
            "DATABASE_URL": runtime_url,
            "ORCH_ORGANIZATION_SLUG": identity.organization_slug,
            "ORCH_ORGANIZATION_NAME": identity.organization_name,
            "ORCH_USER_SUBJECT": identity.user_subject,
        },
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert deleted == ["acme/run-purge/rejected.mp4"]
    assert payload["purged"] == ["acme/run-purge/rejected.mp4"]
    assert payload["inventory"]["object_count"] == 0
    assert payload["health"]["alerts"] == []
