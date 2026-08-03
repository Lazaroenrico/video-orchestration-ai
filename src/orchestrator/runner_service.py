"""HTTP interno do Runner; Queue e cron só acordam o consumidor PostgreSQL."""
from __future__ import annotations

import os
import secrets

from fastapi import FastAPI, Header, HTTPException

import orchestrator.job_store as job_store
from orchestrator.wake_queue import build_wake_queue, publish_outbox_once
from orchestrator.worker import run_worker_once

app = FastAPI(title="UGC Orchestrator Runner")


def _authorize_internal(authorization: str | None) -> None:
    expected = os.environ.get("ORCH_INTERNAL_TOKEN", "")
    if not expected:
        raise RuntimeError("ORCH_INTERNAL_TOKEN é obrigatória no Runner HTTP")
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="token interno inválido")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/runner/once")
async def runner_tick(
    authorization: str | None = Header(default=None),
    worker_id: str = "runner-http",
) -> dict[str, bool]:
    """Drena uma outbox e um job; nunca executa o run dentro do Worker de fila."""
    _authorize_internal(authorization)
    outbox_published = False
    async with job_store.open_repository() as jobs:
        if jobs is not None:
            outbox_published = await publish_outbox_once(
                jobs,
                build_wake_queue(),
                worker_id=worker_id,
            )
    job_processed = await run_worker_once(worker_id=worker_id)
    return {
        "outbox_published": outbox_published,
        "job_processed": job_processed,
    }
