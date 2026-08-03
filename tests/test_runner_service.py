"""Runner HTTP interno usado somente pelo launcher/cron da borda."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from click.testing import CliRunner
from fastapi import HTTPException

from orchestrator import cli as cli_module
from orchestrator import runner_service
from orchestrator.cli import cli


async def test_runner_tick_rejects_missing_or_wrong_internal_token(monkeypatch):
    monkeypatch.setenv("ORCH_INTERNAL_TOKEN", "expected-secret")

    for authorization in (None, "Bearer wrong-secret"):
        with pytest.raises(HTTPException) as error:
            await runner_service.runner_tick(authorization=authorization)
        assert error.value.status_code == 401


async def test_runner_tick_publishes_outbox_then_claims_one_job(monkeypatch):
    monkeypatch.setenv("ORCH_INTERNAL_TOKEN", "expected-secret")
    calls: list[tuple[str, str]] = []
    repository = object()
    queue = object()

    @asynccontextmanager
    async def open_repository():
        yield repository

    async def publish_once(jobs, wake_queue, *, worker_id):
        assert jobs is repository
        assert wake_queue is queue
        calls.append(("outbox", worker_id))
        return True

    async def worker_once(*, worker_id):
        calls.append(("job", worker_id))
        return True

    monkeypatch.setattr(runner_service.job_store, "open_repository", open_repository)
    monkeypatch.setattr(runner_service, "build_wake_queue", lambda: queue)
    monkeypatch.setattr(runner_service, "publish_outbox_once", publish_once)
    monkeypatch.setattr(runner_service, "run_worker_once", worker_once)

    response = await runner_service.runner_tick(
        authorization="Bearer expected-secret",
        worker_id="runner-1",
    )

    assert response == {"outbox_published": True, "job_processed": True}
    assert calls == [("outbox", "runner-1"), ("job", "runner-1")]


async def test_runner_tick_supports_database_sweep_without_repository(monkeypatch):
    monkeypatch.setenv("ORCH_INTERNAL_TOKEN", "expected-secret")

    @asynccontextmanager
    async def open_repository():
        yield None

    async def worker_once(*, worker_id):
        assert worker_id == "runner-http"
        return False

    monkeypatch.setattr(runner_service.job_store, "open_repository", open_repository)
    monkeypatch.setattr(runner_service, "run_worker_once", worker_once)

    response = await runner_service.runner_tick(
        authorization="Bearer expected-secret",
    )

    assert response == {"outbox_published": False, "job_processed": False}


def test_runner_service_requires_an_internal_token(monkeypatch):
    monkeypatch.delenv("ORCH_INTERNAL_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="ORCH_INTERNAL_TOKEN"):
        runner_service._authorize_internal("Bearer anything")


async def test_runner_service_health_is_public():
    assert await runner_service.healthz() == {"status": "ok"}


def test_runner_service_cli_starts_the_internal_asgi_app(monkeypatch):
    calls = []

    def run_uvicorn(host, port, reload, *, application):
        calls.append((host, port, reload, application))

    monkeypatch.setattr(cli_module, "_run_uvicorn", run_uvicorn)

    result = CliRunner().invoke(
        cli,
        ["runner-service", "--host", "127.0.0.1", "--port", "9000"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("127.0.0.1", 9000, False, "orchestrator.runner_service:app")
    ]
