"""Loop de worker durável, independente do backend de wake-up."""
from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
from typing import Any
from typing import Awaitable, Callable

from orchestrator import runner
from orchestrator.config import (
    default_db_path,
    default_media_path,
    load_agent_catalog,
    load_pipeline,
    load_providers,
)
from orchestrator.db import (
    Database,
    Job,
    LeaseLostError,
    PostgresCreatorRepository,
    PostgresJobRepository,
    PostgresRunRepository,
    TenantIdentity,
)
from orchestrator.storage.factory import build_media_storage
from orchestrator.storage.resolve import resolve_signed_uris
from orchestrator.registry import ROLES


JobExecutor = Callable[[Job], Awaitable[None]]


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


async def _record_seed_creator_for_run(
    run_id: str,
    run_payload: dict[str, Any],
    *,
    database: Database,
    tenant: Any,
) -> None:
    seed_creator = run_payload.get("seed_creator")
    if not isinstance(seed_creator, dict):
        return
    if run_payload.get("approve_creators"):
        return
    creator_id = seed_creator.get("id") or seed_creator.get("creator_id")
    if not creator_id:
        return

    canonical_seed = _plain(seed_creator)
    canonical_seed["id"] = str(creator_id)
    await PostgresCreatorRepository(database, tenant).record_creators(
        run_id,
        [canonical_seed],
        approved_ids=[str(creator_id)],
        creator_prompt=run_payload.get("creator_prompt"),
        video_prompt=run_payload.get("video_prompt"),
        offer=run_payload.get("offer"),
    )


async def _execute_pipeline_job(
    job: Job,
    *,
    database: Database,
    tenant: Any,
) -> None:
    if job.kind not in {"execute_run", "resume_run"}:
        raise ValueError(f"job kind desconhecido: {job.kind}")
    event_repository = PostgresJobRepository(database, tenant)

    async def persist_progress(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "progress_event")
        await event_repository.append_event(
            job.run_id,
            event_type,
            {key: value for key, value in event.items() if key != "type"},
        )

    payload = job.payload
    run_payload = payload.get("run") if job.kind == "resume_run" else payload
    if not isinstance(run_payload, dict):
        raise ValueError("payload de run inválido")
    config_dir = run_payload.get("config_dir")
    pipeline = load_pipeline(config_dir)
    providers = load_providers(config_dir)
    paid_enabled = os.environ.get("ORCH_ENABLE_PAID_ADAPTERS", "").lower() in {
        "1",
        "true",
        "yes",
    }
    configured = (providers or {}).get("adapters", {})
    paid_roles = {
        role: configured.get(role, "mock")
        for role in ROLES
        if configured.get(role, "mock") != "mock"
    }
    if paid_roles and not paid_enabled:
        raise RuntimeError(
            "adapters pagos desabilitados na execução durável; defina "
            "ORCH_ENABLE_PAID_ADAPTERS=true somente após habilitar o ledger: "
            f"{paid_roles}"
        )
    agent_catalog = load_agent_catalog(config_dir)
    db_path = Path(run_payload.get("db_path") or default_db_path())
    run_options = {
        key: run_payload.get(key)
        for key in (
            "creator_prompt",
            "video_prompt",
            "approve_creators",
            "edit_concepts",
            "seed_creator",
            "campaign",
            "review_plan",
        )
        if key in run_payload
    }
    if isinstance(run_options.get("seed_creator"), dict):
        await _record_seed_creator_for_run(
            job.run_id,
            run_payload,
            database=database,
            tenant=tenant,
        )
        storage = build_media_storage(
            providers,
            root=default_media_path(),
            web_prefix="/media",
        )
        run_options["seed_creator"] = await resolve_signed_uris(
            run_options["seed_creator"],
            storage=storage,
        )
    if job.kind == "resume_run" and payload.get("gate_type") == "approve_creators":
        gate_payload = payload.get("gate")
        resolution = payload.get("resolution")
        if isinstance(gate_payload, dict) and isinstance(resolution, dict):
            creators = gate_payload.get("creators")
            approved = resolution.get("approved")
            if isinstance(creators, list) and isinstance(approved, list):
                await PostgresCreatorRepository(database, tenant).record_creators(
                    job.run_id,
                    [_plain(creator) for creator in creators],
                    approved_ids=[str(creator_id) for creator_id in approved],
                    creator_prompt=run_payload.get("creator_prompt"),
                    video_prompt=run_payload.get("video_prompt"),
                    offer=run_payload.get("offer"),
                )
    if job.kind == "resume_run" and payload.get("gate_type") == "review_creative_plan":
        gate_payload = payload.get("gate")
        resolution = payload.get("resolution")
        if (
            isinstance(gate_payload, dict)
            and isinstance(resolution, dict)
            and resolution.get("action") == "approve"
        ):
            creators = resolution.get("creators") or gate_payload.get("creators")
            if isinstance(creators, list):
                await PostgresCreatorRepository(database, tenant).record_creators(
                    job.run_id,
                    [_plain(creator) for creator in creators],
                    approved_ids=[
                        str(creator.get("id"))
                        for creator in creators
                        if isinstance(creator, dict) and creator.get("id")
                    ],
                    creator_prompt=None,
                    video_prompt=None,
                    offer=run_payload.get("offer"),
                )
    if job.kind == "execute_run":
        _, output = await runner.run_pipeline(
            pipeline,
            providers,
            db_path=db_path,
            run_id=job.run_id,
            batch=run_payload.get("batch"),
            offer=str(run_payload.get("offer") or "demo offer"),
            platform=str(run_payload.get("platform") or "tiktok"),
            agent_catalog=agent_catalog,
            run_options=run_options,
            event_sink=persist_progress,
        )
    else:
        _, output = await runner.resume_pipeline(
            pipeline,
            providers,
            db_path=db_path,
            run_id=job.run_id,
            platform=str(run_payload.get("platform") or "tiktok"),
            agent_catalog=agent_catalog,
            resume_value=payload.get("resolution"),
            run_options=run_options,
            event_sink=persist_progress,
        )
    persisted_state = await runner.get_status(
        pipeline,
        db_path=db_path,
        run_id=job.run_id,
    )
    state = _plain(persisted_state or output)
    items = [
        _plain(item)
        for item in ((persisted_state or output).get("results") or [])
    ]
    runs = PostgresRunRepository(database, tenant)
    interrupt = await runner.get_interrupt(
        pipeline,
        db_path=db_path,
        run_id=job.run_id,
    )
    if interrupt is not None:
        await runs.save(
            job.run_id,
            phase="running",
            state=state,
            summary=_plain(
                runner.summarize({**(persisted_state or output), "run_id": job.run_id})
            ),
            items=items,
        )
        await PostgresJobRepository(database, tenant).open_gate(
            job.run_id,
            gate_type=str(interrupt.get("type") or "unknown"),
            payload=_plain(interrupt),
        )
        return
    await runs.save(
        job.run_id,
        phase="done",
        state=state,
        summary=_plain(
            runner.summarize({**(persisted_state or output), "run_id": job.run_id})
        ),
        items=items,
    )
    await event_repository.append_event(
        job.run_id,
        "run_end",
        {
            "run_id": job.run_id,
            "summary": _plain(
                runner.summarize({
                    **(persisted_state or output),
                    "run_id": job.run_id,
                })
            ),
        },
    )


async def run_worker_once(
    *,
    worker_id: str,
    execute: JobExecutor | None = None,
    now: datetime | None = None,
    heartbeat_seconds: float = 30,
    database: Database | None = None,
    tenant: Any | None = None,
) -> bool:
    """Processa no máximo um job; devolve ``False`` quando a fila está vazia."""
    if database is not None:
        resolved_tenant = tenant or await database.resolve_tenant(TenantIdentity.from_env())
        return await _run_worker_once_with_database(
            worker_id=worker_id,
            database=database,
            tenant=resolved_tenant,
            execute=execute,
            now=now,
            heartbeat_seconds=heartbeat_seconds,
        )

    async with Database.from_env() as database:
        tenant = await database.resolve_tenant(TenantIdentity.from_env())
        return await _run_worker_once_with_database(
            worker_id=worker_id,
            database=database,
            tenant=tenant,
            execute=execute,
            now=now,
            heartbeat_seconds=heartbeat_seconds,
        )


async def _run_worker_once_with_database(
    *,
    worker_id: str,
    database: Database,
    tenant: Any,
    execute: JobExecutor | None,
    now: datetime | None,
    heartbeat_seconds: float,
) -> bool:
    jobs = PostgresJobRepository(database, tenant)
    claimed = await jobs.claim(worker_id, limit=1, now=now)
    if not claimed:
        return False
    job = claimed[0]
    executor = execute
    if executor is None:
        async def executor(claimed_job: Job) -> None:
            await _execute_pipeline_job(
                claimed_job,
                database=database,
                tenant=tenant,
            )
    stop_heartbeat = asyncio.Event()

    async def renew_lease() -> None:
        while not stop_heartbeat.is_set():
            if heartbeat_seconds > 0:
                try:
                    await asyncio.wait_for(
                        stop_heartbeat.wait(),
                        timeout=heartbeat_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)
                if stop_heartbeat.is_set():
                    break
            await jobs.renew(job.job_id, worker_id=worker_id)

    heartbeat = asyncio.create_task(renew_lease())
    execution = asyncio.create_task(executor(job))
    try:
        finished, _ = await asyncio.wait(
            {execution, heartbeat},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat in finished:
            await heartbeat
        await execution
    except Exception as exc:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        if isinstance(exc, LeaseLostError):
            raise
        await jobs.fail(
            job.job_id,
            worker_id=worker_id,
            error=str(exc),
            now=now,
        )
    else:
        await jobs.complete(
            job.job_id,
            worker_id=worker_id,
            now=now,
        )
    finally:
        stop_heartbeat.set()
        execution.cancel()
        await asyncio.gather(heartbeat, execution, return_exceptions=True)
    return True
