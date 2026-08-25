"""Rotas de ciclo de vida de runs: start (V1/V2), retry, stream SSE, list/status/state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

import orchestrator.job_store as job_store
import orchestrator.prompt_store as prompt_store
import orchestrator.run_store as run_store
from orchestrator import runner
from orchestrator.common.plain import to_plain as _to_plain
from orchestrator.config import default_db_path, default_prompt_store_path
from orchestrator.creative_contracts import CampaignInput
from orchestrator.graph.topology import topology_for_tiers
from orchestrator.nodes.base import tier_names
from orchestrator.progress import build_activity, build_progress
from orchestrator.storage.resolve import resolve_signed_uris
from orchestrator.web import runs_registry
from orchestrator.web.events import (
    DATABASE_UNAVAILABLE_ERRORS,
    _complete_item_payload,
    _item_payload_from_result,
    _merge_item_snapshot,
    _normalize_creator,
    _persisted_event_payload,
    _runtime_phase,
    _safe_serialize,
    _snapshot_from_item,
)
from orchestrator.web.run_executor import _execute_run, _server_attr
from orchestrator.web.settings import effective_config_dir

_log = logging.getLogger(__name__)

router = APIRouter()


class RunRequest(BaseModel):
    offer: str = "demo offer"
    batch: int = 6
    platform: str = "tiktok"
    config_dir: Optional[str] = None
    db: Optional[str] = None
    creator_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    approve_creators: bool = True
    edit_concepts: bool = True
    creator_id: Optional[str] = None
    creator_run_id: Optional[str] = None


class RunV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign: CampaignInput
    config_dir: Optional[str] = None
    db: Optional[str] = None


@router.post("/api/v2/runs")
async def start_run_v2(
    req: RunV2Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    campaign = req.campaign
    run_id = f"web-{uuid.uuid4().hex[:8]}"
    db_path = req.db or str(default_db_path())
    effective = effective_config_dir(req.config_dir)
    payload = {
        "offer": campaign.offer,
        "batch": campaign.batch_size,
        "platform": campaign.platform,
        "config_dir": effective,
        "db_path": db_path,
        "campaign": campaign.model_dump(mode="json"),
        "review_plan": True,
    }
    async with job_store.open_repository() as jobs:
        if jobs is not None:
            queued = await jobs.enqueue_run(
                run_id,
                offer=campaign.offer,
                platform=campaign.platform,
                batch_size=campaign.batch_size,
                payload=payload,
            )
            _server_attr("_wake_web_embedded_runner")()
            return {"run_id": run_id, "job_id": str(queued.job_id)}

    runs_registry.REGISTRY.create(run_id)
    async with run_store.open_repository() as runs:
        if runs is not None:
            await runs.start(
                run_id,
                offer=campaign.offer,
                platform=campaign.platform,
                batch_size=campaign.batch_size,
            )
    background_tasks.add_task(
        _execute_run,
        run_id,
        campaign.offer,
        campaign.batch_size,
        campaign.platform,
        effective,
        db_path,
        None,
        None,
        False,
        False,
        None,
        campaign.model_dump(mode="json"),
        True,
    )
    return {"run_id": run_id}


@router.post("/api/run")
async def start_run(req: RunRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    seed_creator = None
    if req.creator_id:
        seed_creator = await _server_attr("_find_creator_for_draft_repository")(
            req.creator_id,
            req.creator_run_id,
        )
    run_id = f"web-{uuid.uuid4().hex[:8]}"
    db_path = req.db or str(default_db_path())
    effective = effective_config_dir(req.config_dir)
    # Todo run registra o "último prompt usado" por tipo — independente do gate de
    # aprovação (creators.json só persiste prompts quando o gate roda).
    async with prompt_store.open_repository(default_prompt_store_path()) as prompts:
        await prompts.record_last_used(
            creator_prompt=req.creator_prompt,
            video_prompt=req.video_prompt,
        )
    async with job_store.open_repository() as jobs:
        if jobs is not None:
            queued = await jobs.enqueue_run(
                run_id,
                offer=req.offer,
                platform=req.platform,
                batch_size=req.batch,
                payload={
                    "offer": req.offer,
                    "batch": req.batch,
                    "platform": req.platform,
                    "config_dir": effective,
                    "db_path": db_path,
                    "creator_prompt": req.creator_prompt,
                    "video_prompt": req.video_prompt,
                    "approve_creators": req.approve_creators,
                    "edit_concepts": req.edit_concepts,
                    "seed_creator": seed_creator,
                },
            )
            _server_attr("_wake_web_embedded_runner")()
            return {"run_id": run_id, "job_id": str(queued.job_id)}

    # No caminho local, API e executor vivem no mesmo processo. No caminho durável,
    # o job acima guarda só o ponteiro canônico e o Runner assina no consumo.
    if seed_creator is not None:
        seed_creator = await _server_attr("_sign_payload")(seed_creator, effective)
    runs_registry.REGISTRY.create(run_id)
    async with run_store.open_repository() as runs:
        if runs is not None:
            await runs.start(
                run_id,
                offer=req.offer,
                platform=req.platform,
                batch_size=req.batch,
            )
    background_tasks.add_task(
        _execute_run,
        run_id,
        req.offer,
        req.batch,
        req.platform,
        effective,
        db_path,
        req.creator_prompt,
        req.video_prompt,
        req.approve_creators,
        req.edit_concepts,
        seed_creator,
    )
    return {"run_id": run_id}


def _retry_payload_fields(payload: dict[str, Any]) -> tuple[str, str, int]:
    offer = payload.get("offer")
    platform = payload.get("platform")
    batch = payload.get("batch")
    if (
        not isinstance(offer, str)
        or not offer.strip()
        or not isinstance(platform, str)
        or not platform.strip()
        or not isinstance(batch, int)
        or isinstance(batch, bool)
    ):
        raise HTTPException(
            status_code=409,
            detail="payload original indisponível para retry",
        )
    return offer, platform, batch


@router.post("/api/run/{run_id}/retry")
async def retry_run(run_id: str) -> dict[str, str]:
    async with run_store.open_repository() as runs:
        snapshot = await runs.get(run_id) if runs is not None else None
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    if snapshot.phase != "error":
        raise HTTPException(status_code=409, detail="retry exige run em error")

    async with job_store.open_repository() as jobs:
        if jobs is None:
            raise HTTPException(
                status_code=409,
                detail="payload original indisponível para retry",
            )
        original_payload = await jobs.get_initial_run_payload(run_id)
        if original_payload is None:
            raise HTTPException(
                status_code=409,
                detail="payload original indisponível para retry",
            )
        offer, platform, batch = _retry_payload_fields(original_payload)
        new_run_id = f"web-{uuid.uuid4().hex[:8]}"
        payload = {**original_payload, "source_run_id": run_id}
        queued = await jobs.enqueue_run(
            new_run_id,
            offer=offer,
            platform=platform,
            batch_size=batch,
            payload=payload,
        )
        _server_attr("_wake_web_embedded_runner")()
    return {
        "run_id": new_run_id,
        "source_run_id": run_id,
        "job_id": str(queued.job_id),
    }


@router.get("/api/stream/{run_id}")
async def stream_events(
    run_id: str,
    config_dir: Optional[str] = None,
    last_event_id: Optional[str] = Header(
        default=None,
        alias="Last-Event-ID",
    ),
) -> StreamingResponse:
    if os.environ.get("DATABASE_URL"):
        after_seq = 0
        if isinstance(last_event_id, str) and last_event_id:
            try:
                after_seq = int(last_event_id)
            except ValueError as exc:
                raise HTTPException(400, "Last-Event-ID inválido") from exc
        async with run_store.open_repository() as runs:
            persisted = await runs.get(run_id) if runs is not None else None
        if persisted is None:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
        storage = _server_attr("_signing_storage")(config_dir)

        async def generate_persisted():
            cursor = after_seq
            try:
                while True:
                    async with job_store.open_repository() as jobs:
                        assert jobs is not None
                        events = await jobs.list_events(run_id, after_seq=cursor)
                    for event in events:
                        cursor = event.seq
                        payload = await resolve_signed_uris(
                            _persisted_event_payload(event),
                            storage=storage,
                        )
                        yield (
                            f"id: {event.seq}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        )
                    async with run_store.open_repository() as runs:
                        snapshot = await runs.get(run_id) if runs is not None else None
                    if snapshot is not None and snapshot.phase in {
                        "done",
                        "error",
                        "cancelled",
                    }:
                        yield 'data: {"type": "stream_end"}\n\n'
                        return
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except DATABASE_UNAVAILABLE_ERRORS as exc:
                _log.warning(
                    "stream persistido interrompido: banco indisponível (%s)",
                    type(exc).__name__,
                )
                yield (
                    "retry: 30000\n"
                    'data: {"type": "service_unavailable",'
                    '"detail": "Persistence temporarily unavailable"}\n\n'
                )

        return StreamingResponse(
            generate_persisted(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    state = runs_registry.REGISTRY.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    local_after = 0
    if isinstance(last_event_id, str) and last_event_id:
        prefix, separator, raw_sequence = last_event_id.partition("-")
        if prefix != "local" or separator != "-":
            raise HTTPException(400, "Last-Event-ID inválido")
        try:
            local_after = int(raw_sequence)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID inválido") from exc

    q: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=500)

    # Replay eventos já emitidos (para clientes que conectam tarde)
    for event in state["buffer"]:
        event_id = str(event.get("event_id") or "")
        try:
            event_sequence = int(event_id.removeprefix("local-"))
        except ValueError:
            event_sequence = local_after + 1
        if event_sequence > local_after:
            q.put_nowait(event)

    if state["done"]:
        q.put_nowait(None)
    else:
        state["queues"].append(q)

    # Uma vez por stream, não por evento: cada chamada reconstrói o client boto3, e um
    # stream longo emite centenas de eventos. O TTL da URL só começa a correr no yield,
    # então assinar aqui (e não no _emit) é o que mantém o buffer de replay com o
    # ponteiro canônico — URL assinada vence, ponteiro não.
    storage = _server_attr("_signing_storage")(config_dir)

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    yield 'data: {"type": "stream_end"}\n\n'
                    return
                event = await resolve_signed_uris(event, storage=storage)
                event_id = event.get("event_id")
                prefix = f"id: {event_id}\n" if event_id else ""
                yield f"{prefix}data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            qs = runs_registry.REGISTRY.get(run_id, {}).get("queues", [])
            if q in qs:
                qs.remove(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/runs")
async def list_runs_endpoint(db: Optional[str] = None) -> dict[str, Any]:
    db_path = db or str(default_db_path())
    # `active` = só o que está realmente rodando; runs concluídos ou quebrados saem
    # daqui (senão a lista os rotularia "Generating" para sempre). `errored` deixa a
    # UI marcar os que falharam como "Failed".
    persisted_ids: list[str] = []
    persisted_errors: list[str] = []
    persisted_cancelled: list[str] = []
    persisted_active: list[str] = []
    postgres_enabled = False
    async with run_store.open_repository() as runs:
        if runs is not None:
            postgres_enabled = True
            index = await runs.list_index()
            persisted_ids = [entry.run_id for entry in index]
            persisted_errors = [
                entry.run_id
                for entry in index
                if entry.phase != "cancelled" and (entry.phase == "error" or entry.error)
            ]
            persisted_cancelled = [entry.run_id for entry in index if entry.phase == "cancelled"]
            persisted_active = [
                entry.run_id
                for entry in index
                if entry.phase in {"running", "editing", "awaiting", "review"} and not entry.error
            ]
    if postgres_enabled:
        errored = persisted_errors
        cancelled = persisted_cancelled
        active = persisted_active
        known = persisted_ids
    else:
        cancelled = [
            rid
            for rid, state in runs_registry.REGISTRY.items()
            if state.get("phase") == "cancelled"
        ]
        errored = [
            rid
            for rid, state in runs_registry.REGISTRY.items()
            if state.get("error") and state.get("phase") != "cancelled"
        ]
        active = [
            rid
            for rid, state in runs_registry.REGISTRY.items()
            if not state.get("error")
            and state.get("phase") != "cancelled"
            and not state.get("done")
        ]
        known = runner.list_runs(db_path)
    return {
        "runs": known,
        "active": active,
        "errored": errored,
        "cancelled": cancelled,
    }


@router.get("/api/status/{run_id}")
async def run_status(
    run_id: str, config_dir: Optional[str] = None, db: Optional[str] = None
) -> Any:
    pipeline = _server_attr("load_pipeline")(effective_config_dir(config_dir))
    db_path = db or str(default_db_path())
    state = await runner.get_status(pipeline, db_path=db_path, run_id=run_id)
    if state is None:
        async with run_store.open_repository() as runs:
            persisted = await runs.get(run_id) if runs is not None else None
        if persisted is None:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
        return persisted.summary
    return runner.summarize({**state, "run_id": run_id})


@router.get("/api/state/{run_id}")
async def run_state(
    run_id: str, config_dir: Optional[str] = None, db: Optional[str] = None
) -> dict[str, Any]:
    effective = effective_config_dir(config_dir)
    pipeline = _server_attr("load_pipeline")(effective)
    topology = topology_for_tiers(tier_names(pipeline))
    db_path = db or str(default_db_path())
    checkpoint_state = await runner.get_status(pipeline, db_path=db_path, run_id=run_id)
    runtime_state = runs_registry.REGISTRY.get(run_id)
    async with run_store.open_repository() as runs:
        persisted = await runs.get(run_id) if runs is not None else None
    if checkpoint_state is None and runtime_state is None and persisted is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    summary: dict[str, Any] | None = None
    if checkpoint_state is not None:
        summary = runner.summarize({**checkpoint_state, "run_id": run_id})
    elif persisted is not None:
        summary = persisted.summary
    if summary is None and runtime_state is not None:
        for event in reversed(runtime_state.get("buffer") or []):
            if event.get("type") == "run_end" and isinstance(event.get("summary"), dict):
                summary = _safe_serialize(event["summary"])
                break

    checkpoint_results = (checkpoint_state or {}).get("results") or []
    # Itens que quebraram/ficaram em voo nunca entram em `results` (o node levanta antes
    # do write). Recuperamos do checkpoint os `process_item` pendentes — com seus clips e
    # o motivo do erro — para não sumirem da UI. Dedup por id: `results` sempre vence.
    if checkpoint_state is not None:
        existing_ids = {
            str(plain["id"])
            for r in checkpoint_results
            if isinstance((plain := _to_plain(r)), dict) and plain.get("id")
        }
        try:
            pending = await runner.get_pending_items(pipeline, db_path=db_path, run_id=run_id)
        except Exception:  # noqa: BLE001 — recuperação best-effort, nunca derruba o /api/state
            pending = []
        orphans = [p for p in pending if str(getattr(p, "id", "")) not in existing_ids]
        if orphans:
            checkpoint_results = list(checkpoint_results) + orphans
    runtime_snapshots = (
        (runtime_state or {}).get("item_snapshots") if runtime_state is not None else None
    )
    if checkpoint_state is None and persisted is not None and not runtime_snapshots:
        items = [_safe_serialize(item) for item in persisted.items]
    elif isinstance(runtime_snapshots, dict) and runtime_snapshots:
        snapshots: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for result in checkpoint_results:
            snapshot = _snapshot_from_item(result)
            item_id = snapshot.get("id")
            if not item_id:
                continue
            item_id = str(item_id)
            snapshots[item_id] = _merge_item_snapshot(snapshots.get(item_id, {}), snapshot)
            if item_id not in order:
                order.append(item_id)
        for fallback_id, raw_snapshot in runtime_snapshots.items():
            snapshot = _to_plain(raw_snapshot)
            if not isinstance(snapshot, dict):
                continue
            item_id = str(snapshot.get("id") or fallback_id)
            snapshot = {**snapshot, "id": item_id}
            snapshots[item_id] = _merge_item_snapshot(snapshots.get(item_id, {}), snapshot)
            if item_id not in order:
                order.append(item_id)
        items = [_safe_serialize(_complete_item_payload(snapshots[item_id])) for item_id in order]
    else:
        items = [_item_payload_from_result(item) for item in checkpoint_results]

    phase = (
        _runtime_phase(runtime_state, summary)
        if runtime_state is not None
        else persisted.phase
        if persisted is not None
        else _runtime_phase(None, summary)
    )
    pending_gate = None
    persisted_events: list[dict[str, Any]] = []
    if runtime_state is None and persisted is not None:
        async with job_store.open_repository() as jobs:
            if jobs is not None:
                list_events = getattr(jobs, "list_events", None)
                if list_events is not None:
                    persisted_events = [
                        _persisted_event_payload(event) for event in await list_events(run_id)
                    ]
                if phase in {"editing", "awaiting", "review"}:
                    get_pending_gate = getattr(jobs, "get_pending_gate", None)
                    if get_pending_gate is not None:
                        pending_gate = await get_pending_gate(run_id)
    if pending_gate is not None:
        expected_gate_type = {
            "editing": "edit_concepts",
            "awaiting": "approve_creators",
            "review": "review_creative_plan",
        }.get(phase)
        if pending_gate.gate_type != expected_gate_type:
            pending_gate = None
    gate_ref = (
        {
            "gate_id": str(pending_gate.gate_id),
            "version": pending_gate.version,
            "gate_type": pending_gate.gate_type,
        }
        if pending_gate is not None
        else None
    )
    edit_concepts: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    review: dict[str, Any] | None = None
    if runtime_state is not None and phase == "review":
        pending_review = runtime_state.get("pending_review")
        if isinstance(pending_review, dict):
            review = _safe_serialize(pending_review)
    elif persisted is not None and phase == "review":
        review_source = (
            pending_gate.payload
            if pending_gate is not None and pending_gate.gate_type == "review_creative_plan"
            else persisted.state.get("pending_review")
        )
        if isinstance(review_source, dict):
            review = {
                key: _safe_serialize(value)
                for key, value in review_source.items()
                if key not in {"type", "creators"}
            }
            creators_source = review_source.get("creators")
            if isinstance(creators_source, list):
                review["creators"] = [
                    _normalize_creator(creator)
                    for creator in creators_source
                    if isinstance(creator, dict)
                ]
    if runtime_state is not None and phase == "editing":
        edit_concepts = [
            _safe_serialize(c)
            for c in runtime_state.get("pending_concepts") or []
            if isinstance(c, dict)
        ]
    elif persisted is not None and phase == "editing":
        concepts_source = (
            pending_gate.payload.get("concepts")
            if pending_gate is not None and pending_gate.gate_type == "edit_concepts"
            else persisted.state.get("pending_concepts")
        )
        edit_concepts = [_safe_serialize(c) for c in concepts_source or [] if isinstance(c, dict)]
    if runtime_state is not None and phase == "awaiting":
        awaiting = [
            _normalize_creator(c)
            for c in runtime_state.get("pending_creators") or []
            if isinstance(c, dict)
        ]
    elif persisted is not None and phase == "awaiting":
        creators_source = (
            pending_gate.payload.get("creators")
            if pending_gate is not None and pending_gate.gate_type == "approve_creators"
            else persisted.state.get("pending_creators")
        )
        awaiting = [_normalize_creator(c) for c in creators_source or [] if isinstance(c, dict)]

    progress_events = (
        list(runtime_state.get("buffer") or []) if runtime_state is not None else persisted_events
    )
    progress = build_progress(
        progress_events,
        phase=phase,
        items=items,
        batch_size=persisted.batch_size if persisted is not None else None,
        topology=topology,
    )
    activity = build_activity(progress_events, topology=topology)

    return await _server_attr("_sign_payload")(
        {
            "run_id": run_id,
            "phase": phase,
            "items": items,
            "edit_concepts": edit_concepts,
            "awaiting": awaiting,
            "review": review,
            "gate": gate_ref,
            "summary": summary,
            "progress": progress,
            "activity": activity,
            "error": (
                runtime_state.get("error")
                if runtime_state is not None
                else persisted.error
                if persisted is not None
                else None
            ),
        },
        effective,
    )
