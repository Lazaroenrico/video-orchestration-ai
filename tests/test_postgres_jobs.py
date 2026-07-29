"""Jobs, eventos e outbox duráveis da ADR-D36, Fase 3."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import BackgroundTasks

from orchestrator.db import (
    CancelledGateError,
    Database,
    Job,
    LeaseLostError,
    PostgresCreatorRepository,
    PostgresJobRepository,
    PostgresRunRepository,
    StaleGateError,
    TenantIdentity,
    provision_runtime_role,
    upgrade_database,
)
from orchestrator.web import server as web_server
from orchestrator import runner as runner_module
from orchestrator import worker as worker_module
from orchestrator.worker import run_worker_once
from orchestrator.wake_queue import publish_outbox_once


NOW = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


def _database_url(postgresql, user=None, password=None):
    info = postgresql.info
    credentials = user or info.user
    if password is not None:
        credentials = f"{credentials}:{password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


def _runtime_url(postgresql):
    admin_url = _database_url(postgresql)
    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    return _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )


async def test_enqueued_run_is_claimed_completed_and_replayable_after_restart(
    postgresql,
):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("jobs-acme", "Jobs Acme", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        queued = await jobs.enqueue_run(
            "run-1",
            offer="serum X",
            platform="tiktok",
            batch_size=3,
            payload={"config_dir": "config-mock"},
            now=NOW,
        )
        persisted_run = await PostgresRunRepository(database, tenant).get("run-1")
        claimed = await jobs.claim("runner-a", limit=1, now=NOW)
        await jobs.complete(claimed[0].job_id, worker_id="runner-a", now=NOW)

    async with Database(runtime_url) as restarted_database:
        tenant = await restarted_database.ensure_tenant(identity)
        restarted = PostgresJobRepository(restarted_database, tenant)
        completed = await restarted.get(queued.job_id)
        events = await restarted.list_events("run-1")
        outbox = await restarted.list_outbox()

    assert persisted_run is not None
    assert persisted_run.phase == "running"
    assert queued.status == "queued"
    assert [(job.status, job.worker_id, job.attempt) for job in claimed] == [
        ("running", "runner-a", 1)
    ]
    assert completed is not None and completed.status == "succeeded"
    assert [event.event_type for event in events] == [
        "run_queued",
        "run_start",
        "job_started",
        "job_succeeded",
    ]
    assert [(entry.topic, entry.status) for entry in outbox] == [
        ("run.queued", "pending")
    ]


async def test_initial_run_payload_is_retrievable_for_manual_retry(postgresql):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("retry-jobs", "Retry Jobs", "oidc|runner")
    payload = {
        "offer": "serum X",
        "batch": 2,
        "platform": "tiktok",
        "config_dir": "config-mock",
        "db_path": "/tmp/orchestrator.db",
        "creator_prompt": "creator prompt",
        "video_prompt": "video prompt",
        "approve_creators": False,
        "edit_concepts": True,
        "seed_creator": {"id": "creator-fixed"},
    }

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        await jobs.enqueue_run(
            "run-retry-source",
            offer="serum X",
            platform="tiktok",
            batch_size=2,
            payload=payload,
            now=NOW,
        )
        initial = await jobs.get_initial_run_payload("run-retry-source")
        missing = await jobs.get_initial_run_payload("missing-run")

    assert initial == payload
    assert missing is None


async def test_job_lease_renews_and_failures_retry_with_bounded_backoff(
    postgresql,
):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("retry-acme", "Retry Acme", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        queued = await jobs.enqueue_run(
            "run-retry",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            max_attempts=2,
            now=NOW,
        )
        first = (await jobs.claim("runner-a", now=NOW))[0]
        renewed = await jobs.renew(
            first.job_id,
            worker_id="runner-a",
            now=NOW + timedelta(seconds=30),
        )
        with pytest.raises(LeaseLostError, match="lease perdido"):
            await jobs.renew(
                first.job_id,
                worker_id="runner-b",
                now=NOW + timedelta(seconds=30),
            )
        retrying = await jobs.fail(
            first.job_id,
            worker_id="runner-a",
            error="transient",
            now=NOW + timedelta(seconds=31),
        )
        too_early = await jobs.claim(
            "runner-b",
            now=NOW + timedelta(seconds=35),
        )
        second = (
            await jobs.claim(
                "runner-b",
                now=NOW + timedelta(seconds=36),
            )
        )[0]
        failed = await jobs.fail(
            second.job_id,
            worker_id="runner-b",
            error="still broken",
            now=NOW + timedelta(seconds=37),
        )
        run = await PostgresRunRepository(database, tenant).get("run-retry")
        events = await jobs.list_events("run-retry")

    assert queued.max_attempts == 2
    assert renewed.lease_expires_at == NOW + timedelta(seconds=150)
    assert retrying.status == "retry"
    assert retrying.available_at == NOW + timedelta(seconds=36)
    assert too_early == []
    assert second.attempt == 2
    assert failed.status == "failed"
    assert failed.error == "still broken"
    assert run is not None and run.phase == "error"
    assert [event.event_type for event in events] == [
        "run_queued",
        "run_start",
        "job_started",
        "job_lease_renewed",
        "job_retry",
        "job_started",
        "job_failed",
        "error",
    ]


async def test_gate_resolution_is_versioned_and_enqueues_exactly_one_resume(
    postgresql,
):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("gates-acme", "Gates Acme", "oidc|reviewer")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        await jobs.enqueue_run(
            "run-gate",
            offer="serum X",
            platform="tiktok",
            batch_size=2,
            payload={},
            now=NOW,
        )
        gate = await jobs.open_gate(
            "run-gate",
            gate_type="approve_creators",
            payload={"creators": [{"id": "creator-0"}]},
            now=NOW,
        )

    async with Database(runtime_url) as restarted_database:
        tenant = await restarted_database.ensure_tenant(identity)
        restarted = PostgresJobRepository(restarted_database, tenant)
        pending = await restarted.get_pending_gate("run-gate")
        resume = await restarted.resolve_gate(
            gate.gate_id,
            version=gate.version,
            resolution={"approved": ["creator-0"]},
            now=NOW + timedelta(seconds=1),
        )
        with pytest.raises(StaleGateError, match="stale"):
            await restarted.resolve_gate(
                gate.gate_id,
                version=gate.version,
                resolution={"approved": []},
                now=NOW + timedelta(seconds=2),
            )
        next_gate = await restarted.open_gate(
            "run-gate",
            gate_type="approve_creators",
            payload={"creators": [{"id": "creator-1"}]},
            now=NOW + timedelta(seconds=3),
        )
        events = await restarted.list_events("run-gate")

    assert pending == gate
    assert gate.status == "pending"
    assert gate.version == 1
    assert resume.status == "queued"
    assert resume.kind == "resume_run"
    assert resume.payload["gate_id"] == str(gate.gate_id)
    assert resume.payload["gate_version"] == 1
    assert resume.payload["resolution"] == {"approved": ["creator-0"]}
    assert next_gate.version == 2
    assert [event.event_type for event in events][-4:] == [
        "awaiting_approval",
        "gate_resolved",
        "gate_opened",
        "awaiting_approval",
    ]
    first_public_gate = events[3].data
    assert first_public_gate["gate_id"] == str(gate.gate_id)
    assert first_public_gate["version"] == 1
    assert first_public_gate["gate_type"] == "approve_creators"


async def test_pipeline_v2_cancellation_is_tenant_scoped_and_idempotent(postgresql):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("cancel-v1", "Cancel V1", "oidc|reviewer")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        queued = await jobs.enqueue_run(
            "run-v1-gate",
            offer="serum X",
            platform="tiktok",
            batch_size=2,
            payload={},
            now=NOW,
        )
        gate = await jobs.open_gate(
            "run-v1-gate",
            gate_type="approve_creators",
            payload={"creators": [{"id": "creator-0"}]},
            now=NOW,
        )

        summary = await jobs.cancel_pending_test_runs(now=NOW + timedelta(seconds=1))
        repeated = await jobs.cancel_pending_test_runs(now=NOW + timedelta(seconds=2))
        cancelled_job = await jobs.get(queued.job_id)
        cancelled_run = await PostgresRunRepository(database, tenant).get("run-v1-gate")
        events = await jobs.list_events("run-v1-gate")

        with pytest.raises(CancelledGateError, match="cancelado"):
            await jobs.resolve_gate(
                gate.gate_id,
                version=gate.version,
                resolution={"approved": ["creator-0"]},
                now=NOW + timedelta(seconds=3),
            )

    assert summary.gates == 1
    assert summary.runs == 1
    assert summary.jobs == 1
    assert repeated.gates == repeated.runs == repeated.jobs == 0
    assert cancelled_job is not None and cancelled_job.status == "cancelled"
    assert cancelled_run is not None and cancelled_run.phase == "cancelled"
    assert events[-1].event_type == "run_cancelled"
    assert events[-1].data["reason"] == "pipeline_v2_reset"


async def test_post_run_enqueues_durable_job_without_background_task(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "api-jobs")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "API Jobs")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|api")
    background = BackgroundTasks()

    response = await web_server.start_run(
        web_server.RunRequest(
            offer="serum X",
            batch=2,
            config_dir="config-mock",
            approve_creators=False,
            edit_concepts=False,
        ),
        background,
    )

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        queued = await PostgresJobRepository(database, tenant).get(
            UUID(response["job_id"])
        )

    assert background.tasks == []
    assert queued is not None
    assert queued.run_id == response["run_id"]
    assert queued.payload["offer"] == "serum X"
    assert response["run_id"] not in web_server._runs


async def test_worker_once_claims_completes_and_then_reports_idle(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "worker-jobs")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Worker Jobs")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|worker")
    observed = []

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        queued = await PostgresJobRepository(database, tenant).enqueue_run(
            "run-worker",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            now=NOW,
        )

    async def execute(job):
        observed.append(job.job_id)

    worked = await run_worker_once(
        worker_id="runner-a",
        execute=execute,
        now=NOW,
    )
    idle = await run_worker_once(
        worker_id="runner-a",
        execute=execute,
        now=NOW,
    )

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        completed = await PostgresJobRepository(database, tenant).get(queued.job_id)

    assert worked is True
    assert idle is False
    assert observed == [queued.job_id]
    assert completed is not None and completed.status == "succeeded"


async def test_default_worker_executes_mock_pipeline_and_persists_run(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "pipeline-worker")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Pipeline Worker")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|pipeline-worker")
    response = await web_server.start_run(
        web_server.RunRequest(
            offer="serum X",
            batch=1,
            config_dir="config-mock",
            approve_creators=False,
            edit_concepts=False,
        ),
        BackgroundTasks(),
    )

    worked = await run_worker_once(worker_id="runner-pipeline")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        run = await PostgresRunRepository(database, tenant).get(response["run_id"])
        job = await PostgresJobRepository(database, tenant).get(
            UUID(response["job_id"])
        )

    assert worked is True
    assert job is not None and job.status == "succeeded"
    assert run is not None and run.phase == "done"
    assert run.summary["produced"] == 1


async def test_creator_gate_survives_worker_restart_and_http_resolution(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "approval-worker")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Approval Worker")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|reviewer")
    response = await web_server.start_run(
        web_server.RunRequest(
            offer="serum X",
            batch=1,
            config_dir="config-mock",
            approve_creators=True,
            edit_concepts=False,
        ),
        BackgroundTasks(),
    )

    await run_worker_once(worker_id="runner-before-gate")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        jobs = PostgresJobRepository(database, tenant)
        gate = await jobs.get_pending_gate(response["run_id"])
        waiting_run = await PostgresRunRepository(database, tenant).get(
            response["run_id"]
        )

    assert gate is not None
    assert gate.gate_type == "approve_creators"
    assert waiting_run is not None and waiting_run.phase == "awaiting"
    approved_id = gate.payload["creators"][0]["id"]
    resolved = await web_server.approve(
        response["run_id"],
        web_server.ApproveRequest(
            gate_id=str(gate.gate_id),
            version=gate.version,
            approved=[approved_id],
        ),
    )
    with pytest.raises(web_server.HTTPException) as stale:
        await web_server.approve(
            response["run_id"],
            web_server.ApproveRequest(
                gate_id=str(gate.gate_id),
                version=gate.version,
                approved=[],
            ),
        )

    await run_worker_once(worker_id="runner-after-gate")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        finished_run = await PostgresRunRepository(database, tenant).get(
            response["run_id"]
        )
        resume_job = await PostgresJobRepository(database, tenant).get(
            UUID(resolved["job_id"])
        )

    assert stale.value.status_code == 409
    assert resume_job is not None and resume_job.status == "succeeded"
    assert finished_run is not None and finished_run.phase == "done"


async def test_concept_gate_resumes_from_versioned_http_decision(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "concept-worker")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Concept Worker")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|editor")
    response = await web_server.start_run(
        web_server.RunRequest(
            offer="serum X",
            batch=1,
            config_dir="config-mock",
            approve_creators=False,
            edit_concepts=True,
        ),
        BackgroundTasks(),
    )
    await run_worker_once(worker_id="runner-before-edit")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        gate = await PostgresJobRepository(database, tenant).get_pending_gate(
            response["run_id"]
        )

    assert gate is not None and gate.gate_type == "edit_concepts"
    concepts = gate.payload["concepts"]
    concepts[0]["script"] = "edited script"
    resolved = await web_server.submit_concepts(
        response["run_id"],
        web_server.ConceptEditRequest(
            gate_id=str(gate.gate_id),
            version=gate.version,
            concepts=concepts,
        ),
    )
    with pytest.raises(web_server.HTTPException) as stale:
        await web_server.submit_concepts(
            response["run_id"],
            web_server.ConceptEditRequest(
                gate_id=str(gate.gate_id),
                version=gate.version,
                concepts=concepts,
            ),
        )
    await run_worker_once(worker_id="runner-after-edit")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        run = await PostgresRunRepository(database, tenant).get(response["run_id"])

    assert resolved["count"] == 1
    assert "job_id" in resolved
    assert stale.value.status_code == 409
    assert run is not None and run.phase == "done"


async def test_sse_replays_persisted_events_after_last_event_id(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "sse-worker")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "SSE Worker")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|viewer")
    response = await web_server.start_run(
        web_server.RunRequest(
            offer="serum X",
            batch=1,
            config_dir="config-mock",
            approve_creators=False,
            edit_concepts=False,
        ),
        BackgroundTasks(),
    )
    await run_worker_once(worker_id="runner-sse")
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        events = await PostgresJobRepository(database, tenant).list_events(
            response["run_id"]
        )

    stream = await web_server.stream_events(
        response["run_id"],
        last_event_id=str(events[0].seq),
    )
    chunks = [
        chunk.decode() if isinstance(chunk, bytes) else chunk
        async for chunk in stream.body_iterator
    ]
    body = "".join(chunks)

    assert '"type": "run_queued"' not in body
    assert '"type": "run_start"' in body
    assert '"type": "run_end"' in body
    assert "\nevent:" not in body
    assert f"id: {events[1].seq}" in body
    assert '"type": "stream_end"' in body


async def test_outbox_wakes_queue_once_and_persists_delivery(
    postgresql,
):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("outbox-acme", "Outbox Acme", "oidc|publisher")
    delivered = []

    class RecordingWakeQueue:
        async def publish(self, *, topic, message_key, payload):
            delivered.append((topic, message_key, payload))

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        queued = await jobs.enqueue_run(
            "run-outbox",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            now=NOW,
        )
        first = await publish_outbox_once(
            jobs,
            RecordingWakeQueue(),
            worker_id="publisher-a",
            now=NOW,
        )
        second = await publish_outbox_once(
            jobs,
            RecordingWakeQueue(),
            worker_id="publisher-a",
            now=NOW,
        )
        outbox = await jobs.list_outbox()

    assert first is True
    assert second is False
    assert delivered == [
        (
            "run.queued",
            str(queued.job_id),
            {"job_id": str(queued.job_id), "run_id": "run-outbox"},
        )
    ]
    assert outbox[0].status == "published"


async def test_concurrent_workers_claim_each_job_once_and_recover_expired_lease(
    postgresql,
):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("claim-acme", "Claim Acme", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        first = await jobs.enqueue_run(
            "run-claim-1",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            now=NOW,
        )
        second = await jobs.enqueue_run(
            "run-claim-2",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            now=NOW,
        )
        claims = await asyncio.gather(
            jobs.claim("runner-a", now=NOW),
            jobs.claim("runner-b", now=NOW),
        )
        claimed = [job for batch in claims for job in batch]
        recovered = await jobs.claim(
            "runner-recovery",
            now=NOW + timedelta(seconds=121),
        )

    assert {job.job_id for job in claimed} == {first.job_id, second.job_id}
    assert len(claimed) == 2
    assert len(recovered) == 1
    assert recovered[0].job_id in {first.job_id, second.job_id}
    assert recovered[0].attempt == 2
    assert recovered[0].worker_id == "runner-recovery"


async def test_worker_renews_lease_while_executor_is_running(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "heartbeat-acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Heartbeat Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        await PostgresJobRepository(database, tenant).enqueue_run(
            "run-heartbeat",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
        )

    lease_renewed = asyncio.Event()
    original_renew = PostgresJobRepository.renew

    async def observed_renew(self, *args, **kwargs):
        renewed = await original_renew(self, *args, **kwargs)
        lease_renewed.set()
        return renewed

    monkeypatch.setattr(PostgresJobRepository, "renew", observed_renew)

    async def wait_for_heartbeat(_job):
        await asyncio.wait_for(lease_renewed.wait(), timeout=2)

    worked = await run_worker_once(
        worker_id="runner-heartbeat",
        execute=wait_for_heartbeat,
        heartbeat_seconds=0,
    )

    assert worked is True
    assert lease_renewed.is_set()


async def test_worker_stops_heartbeat_without_cancelling_inflight_renew(monkeypatch):
    job = Job(
        job_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id="run-heartbeat-stop",
        kind="execute_run",
        status="running",
        payload={},
        attempt=1,
        max_attempts=1,
        available_at=NOW,
        lease_expires_at=None,
        worker_id="runner-heartbeat-stop",
        error=None,
    )
    renew_started = asyncio.Event()
    release_renew = asyncio.Event()
    renew_cancelled = False
    completed = False

    class FakeDatabase:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def resolve_tenant(self, _identity):
            return object()

    class FakeDatabaseFactory:
        @staticmethod
        def from_env():
            return FakeDatabase()

    class FakeJobs:
        def __init__(self, *_args):
            pass

        async def claim(self, *_args, **_kwargs):
            return [job]

        async def renew(self, *_args, **_kwargs):
            nonlocal renew_cancelled
            renew_started.set()
            try:
                await release_renew.wait()
            except asyncio.CancelledError:
                renew_cancelled = True
                raise
            return job

        async def complete(self, *_args, **_kwargs):
            nonlocal completed
            completed = True

        async def fail(self, *_args, **_kwargs):
            raise AssertionError("job should not fail")

    async def finish_after_renew_starts(_job):
        await asyncio.wait_for(renew_started.wait(), timeout=1)

    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "heartbeat-stop")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Heartbeat Stop")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|heartbeat-stop")
    monkeypatch.setattr(worker_module, "Database", FakeDatabaseFactory)
    monkeypatch.setattr(worker_module, "PostgresJobRepository", FakeJobs)

    task = asyncio.create_task(
        run_worker_once(
            worker_id="runner-heartbeat-stop",
            execute=finish_after_renew_starts,
            heartbeat_seconds=0,
        )
    )
    await asyncio.wait_for(renew_started.wait(), timeout=1)
    await asyncio.sleep(0.05)
    if not task.done():
        release_renew.set()

    worked = await asyncio.wait_for(task, timeout=1)

    assert worked is True
    assert completed is True
    assert renew_cancelled is False


async def test_worker_cancels_execution_immediately_when_heartbeat_loses_lease(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "lost-heartbeat")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Lost Heartbeat")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|runner")
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        await PostgresJobRepository(database, tenant).enqueue_run(
            "run-lost-heartbeat",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
        )

    async def lose_lease(*_args, **_kwargs):
        raise LeaseLostError("lease perdido no heartbeat")

    cancelled = asyncio.Event()

    async def long_execution(_job):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(PostgresJobRepository, "renew", lose_lease)

    started = asyncio.get_running_loop().time()
    with pytest.raises(LeaseLostError, match="heartbeat"):
        await asyncio.wait_for(
            run_worker_once(
                worker_id="runner-lost",
                execute=long_execution,
                heartbeat_seconds=0,
            ),
            timeout=1,
        )

    assert cancelled.is_set()
    assert asyncio.get_running_loop().time() - started < 0.5


async def test_runs_endpoint_derives_active_phase_from_postgres(
    postgresql,
    monkeypatch,
    tmp_path,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "index-acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Index Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|viewer")
    response = await web_server.start_run(
        web_server.RunRequest(
            offer="serum X",
            batch=1,
            config_dir="config-mock",
            approve_creators=False,
            edit_concepts=False,
        ),
        BackgroundTasks(),
    )
    web_server._runs.clear()

    index = await web_server.list_runs_endpoint(db=str(tmp_path / "missing.sqlite"))

    assert response["run_id"] in index["runs"]
    assert response["run_id"] in index["active"]
    assert index["errored"] == []


async def test_api_persists_canonical_creator_pointer_for_worker_handoff(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "creator-handoff")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Creator Handoff")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|api")
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        await PostgresCreatorRepository(database, tenant).record_creators(
            "source-run",
            [{
                "id": "creator-0",
                "image_uri": "r2://ugc/source-run/creator-0.webp",
                "voice_ref": "voice-0",
            }],
            approved_ids=["creator-0"],
        )

    async def signing_must_not_happen_in_api(*_args, **_kwargs):
        raise AssertionError("API não deve persistir signed URL")

    monkeypatch.setattr(web_server, "_sign_payload", signing_must_not_happen_in_api)
    response = await web_server.start_run(
        web_server.RunRequest(
            creator_id="creator-0",
            creator_run_id="source-run",
            config_dir="config-mock",
            approve_creators=False,
            edit_concepts=False,
        ),
        BackgroundTasks(),
    )

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        queued = await PostgresJobRepository(database, tenant).get(
            UUID(response["job_id"])
        )

    assert queued is not None
    assert queued.payload["seed_creator"]["image_uri"] == (
        "r2://ugc/source-run/creator-0.webp"
    )


async def test_worker_signs_canonical_creator_only_at_provider_boundary(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "provider-handoff")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Provider Handoff")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|runner")
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        await PostgresJobRepository(database, tenant).enqueue_run(
            "run-provider-handoff",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={
                "config_dir": "config-mock",
                "offer": "serum X",
                "approve_creators": False,
                "seed_creator": {
                    "id": "creator-0",
                    "image_uri": "r2://ugc/source-run/creator-0.webp",
                    "voice_preview_uri": "r2://ugc/source-run/creator-0.wav",
                },
            },
        )

    class SigningStorage:
        async def get_signed_url(self, key, *, ttl_seconds=900):
            return f"https://provider.example/{key}?ttl={ttl_seconds}"

    observed = {}

    async def fake_run_pipeline(*_args, run_options, **_kwargs):
        observed["seed_creator"] = run_options["seed_creator"]
        return "run-provider-handoff", {"results": []}

    async def fake_status(*_args, **_kwargs):
        return {"results": []}

    async def no_interrupt(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        worker_module,
        "build_media_storage",
        lambda *_args, **_kwargs: SigningStorage(),
        raising=False,
    )
    monkeypatch.setattr(worker_module.runner, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(worker_module.runner, "get_status", fake_status)
    monkeypatch.setattr(worker_module.runner, "get_interrupt", no_interrupt)
    monkeypatch.setattr(worker_module.runner, "summarize", lambda _state: {})

    await run_worker_once(worker_id="runner-provider")

    assert observed["seed_creator"]["image_uri"] == (
        "https://provider.example/source-run/creator-0.webp?ttl=900"
    )
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        persisted = await PostgresCreatorRepository(database, tenant).find_creator(
            "creator-0", "run-provider-handoff"
        )

    assert persisted is not None
    assert persisted["image_uri"] == "r2://ugc/source-run/creator-0.webp"
    assert persisted["voice_preview_uri"] == "r2://ugc/source-run/creator-0.wav"
    assert persisted["status"] == "approved"


async def test_jobs_events_and_outbox_are_isolated_between_organizations(
    postgresql,
):
    runtime_url = _runtime_url(postgresql)
    first_identity = TenantIdentity("org-first", "Org First", "oidc|first")
    second_identity = TenantIdentity("org-second", "Org Second", "oidc|second")

    async with Database(runtime_url) as database:
        first_tenant = await database.ensure_tenant(first_identity)
        second_tenant = await database.ensure_tenant(second_identity)
        first_jobs = PostgresJobRepository(database, first_tenant)
        second_jobs = PostgresJobRepository(database, second_tenant)
        first = await first_jobs.enqueue_run(
            "same-run",
            offer="first",
            platform="tiktok",
            batch_size=1,
            payload={"organization": "first"},
        )
        second = await second_jobs.enqueue_run(
            "same-run",
            offer="second",
            platform="tiktok",
            batch_size=1,
            payload={"organization": "second"},
        )

        assert await first_jobs.get(second.job_id) is None
        assert await second_jobs.get(first.job_id) is None
        first_events = await first_jobs.list_events("same-run")
        assert [
            event.data["job_id"]
            for event in first_events
            if event.event_type == "run_queued"
        ] == [str(first.job_id)]
        assert [entry.message_key for entry in await second_jobs.list_outbox()] == [
            str(second.job_id)
        ]


async def test_outbox_failure_retries_then_enters_operational_dlq(postgresql):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("dlq-acme", "DLQ Acme", "oidc|publisher")

    class BrokenWakeQueue:
        async def publish(self, **_message):
            raise RuntimeError("broker unavailable")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        await jobs.enqueue_run(
            "run-dlq",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            now=NOW,
        )
        for offset in (0, 5, 15, 35, 75):
            with pytest.raises(RuntimeError, match="broker unavailable"):
                await publish_outbox_once(
                    jobs,
                    BrokenWakeQueue(),
                    worker_id="publisher-dlq",
                    now=NOW + timedelta(seconds=offset),
                )
        outbox = await jobs.list_outbox()

    assert len(outbox) == 1
    assert outbox[0].status == "failed"
    assert outbox[0].attempt == 5
    assert outbox[0].error == "broker unavailable"


async def test_migration_from_0007_preserves_runs_and_adds_durable_queue(
    postgresql,
):
    admin_url = _database_url(postgresql)
    runtime_url = _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )
    identity = TenantIdentity("upgrade-acme", "Upgrade Acme", "oidc|runner")
    upgrade_database(admin_url, "20260722_0007")
    provision_runtime_role(admin_url, "runtime-test-secret")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        await PostgresRunRepository(database, tenant).start(
            "run-before-queue",
            offer="preserved offer",
            platform="tiktok",
            batch_size=1,
        )

    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        preserved = await PostgresRunRepository(database, tenant).get(
            "run-before-queue"
        )
        queued = await PostgresJobRepository(database, tenant).enqueue_run(
            "run-before-queue",
            offer="ignored replacement",
            platform="tiktok",
            batch_size=1,
            payload={},
        )

    assert preserved is not None and preserved.offer == "preserved offer"
    assert queued.status == "queued"


async def test_worker_refuses_paid_adapters_until_explicitly_enabled(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "dry-run-acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Dry Run Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|runner")
    monkeypatch.delenv("ORCH_ENABLE_PAID_ADAPTERS", raising=False)
    called = False

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        queued = await PostgresJobRepository(database, tenant).enqueue_run(
            "run-paid-disabled",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={"config_dir": "config"},
        )

    async def must_not_execute(*_args, **_kwargs):
        nonlocal called
        called = True
        return "run-paid-disabled", {"results": []}

    monkeypatch.setattr(worker_module.runner, "run_pipeline", must_not_execute)
    await run_worker_once(worker_id="runner-dry-run")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        job = await PostgresJobRepository(database, tenant).get(queued.job_id)

    assert called is False
    assert job is not None and job.status == "retry"
    assert "ORCH_ENABLE_PAID_ADAPTERS" in (job.error or "")


async def test_lease_guards_reject_stale_job_gate_and_outbox_writers(postgresql):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("lease-guards", "Lease Guards", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        jobs = PostgresJobRepository(database, tenant)
        queued = await jobs.enqueue_run(
            "run-guards",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
            now=NOW,
        )
        await jobs.claim("runner-owner", now=NOW)
        with pytest.raises(LeaseLostError, match="lease perdido"):
            await jobs.complete(
                queued.job_id,
                worker_id="runner-stale",
                now=NOW,
            )
        with pytest.raises(LeaseLostError, match="lease perdido"):
            await jobs.fail(
                queued.job_id,
                worker_id="runner-stale",
                error="stale",
                now=NOW,
            )
        with pytest.raises(ValueError, match="inexistente"):
            await jobs.open_gate(
                "missing-run",
                gate_type="approve_creators",
                payload={},
                now=NOW,
            )

        outbox = (await jobs.claim_outbox("publisher-owner", now=NOW))[0]
        with pytest.raises(LeaseLostError, match="lease perdido"):
            await jobs.mark_outbox_published(
                outbox.entry_id,
                worker_id="publisher-stale",
                now=NOW,
            )
        with pytest.raises(LeaseLostError, match="lease perdido"):
            await jobs.fail_outbox(
                outbox.entry_id,
                worker_id="publisher-stale",
                error="stale",
                now=NOW,
            )


async def test_worker_rejects_unknown_kind_and_malformed_resume_payload():
    common = {
        "job_id": UUID("00000000-0000-0000-0000-000000000001"),
        "run_id": "run-invalid",
        "status": "running",
        "attempt": 1,
        "max_attempts": 1,
        "available_at": NOW,
        "lease_expires_at": NOW + timedelta(seconds=120),
        "worker_id": "runner",
        "error": None,
    }
    unknown = Job(kind="unknown", payload={}, **common)
    malformed = Job(kind="resume_run", payload={"run": "invalid"}, **common)

    with pytest.raises(ValueError, match="kind desconhecido"):
        await worker_module._execute_pipeline_job(
            unknown,
            database=None,
            tenant=None,
        )
    with pytest.raises(ValueError, match="payload de run inválido"):
        await worker_module._execute_pipeline_job(
            malformed,
            database=None,
            tenant=None,
        )


async def test_get_interrupt_handles_an_empty_checkpoint_snapshot(monkeypatch):
    class CheckpointerContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return None

    class EmptyGraph:
        async def aget_state(self, _config):
            return None

    monkeypatch.setattr(
        runner_module,
        "open_checkpointer",
        lambda _path: CheckpointerContext(),
    )
    monkeypatch.setattr(
        runner_module,
        "build_graph",
        lambda _pipeline, checkpointer: EmptyGraph(),
    )

    assert await runner_module.get_interrupt(
        {},
        db_path="unused",
        run_id="run-empty",
    ) is None


async def test_postgres_gate_and_stream_inputs_fail_closed(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "input-guards")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Input Guards")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|reviewer")

    with pytest.raises(web_server.HTTPException) as approval:
        await web_server.approve(
            "run-missing",
            web_server.ApproveRequest(approved=[]),
        )
    with pytest.raises(web_server.HTTPException) as concepts:
        await web_server.submit_concepts(
            "run-missing",
            web_server.ConceptEditRequest(concepts=[]),
        )
    with pytest.raises(web_server.HTTPException) as invalid_cursor:
        await web_server.stream_events(
            "run-missing",
            last_event_id="not-a-sequence",
        )
    with pytest.raises(web_server.HTTPException) as missing_run:
        await web_server.stream_events("run-missing")

    assert approval.value.status_code == 409
    assert concepts.value.status_code == 409
    assert invalid_cursor.value.status_code == 400
    assert missing_run.value.status_code == 404


async def test_persisted_sse_polls_until_running_run_finishes(
    postgresql,
    monkeypatch,
):
    runtime_url = _runtime_url(postgresql)
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "sse-poll")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "SSE Poll")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|viewer")
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        await PostgresJobRepository(database, tenant).enqueue_run(
            "run-sse-poll",
            offer="serum X",
            platform="tiktok",
            batch_size=1,
            payload={},
        )

    sleeps = 0

    async def finish_on_poll(_seconds):
        nonlocal sleeps
        sleeps += 1
        async with Database(runtime_url) as database:
            tenant = await database.ensure_tenant(TenantIdentity.from_env())
            await PostgresRunRepository(database, tenant).save(
                "run-sse-poll",
                phase="done",
                state={},
                summary={},
                items=[],
            )

    monkeypatch.setattr(web_server.asyncio, "sleep", finish_on_poll)
    response = await web_server.stream_events("run-sse-poll")
    body = "".join([
        chunk.decode() if isinstance(chunk, bytes) else chunk
        async for chunk in response.body_iterator
    ])

    assert sleeps == 1
    assert '"type": "stream_end"' in body
