"""Servidor web FastAPI com SSE para visualização em tempo real da pipeline.

Endpoints:
  GET  /                       → dashboard HTML
  POST /api/run                → inicia um run (background task), retorna run_id
  GET  /api/stream/{run_id}    → SSE: eventos de progresso + tokens LLM
  GET  /api/runs               → lista de runs conhecidos
  GET  /api/status/{run_id}    → snapshot do estado atual
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from langgraph.types import Command

from orchestrator import runner, stream_bus
import orchestrator.creator_store as creator_store
from orchestrator.config import (
    default_creator_store_path,
    default_db_path,
    default_media_path,
    load_pipeline,
    load_providers,
)
from orchestrator.tracing import run_trace_config
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.registry import build_adapter_from_providers

app = FastAPI(title="UGC Orchestrator")

# Serve os bytes persistidos do creator (imagem/voz baixadas pelo media_store) em
# /media/{run_id}/{creator_id}/...; _is_renderable_uri já trata esses paths.
_media_root = default_media_path()
_media_root.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(_media_root)), name="media")

# run_id → {queues: list[Queue], buffer: list[dict], done: bool}
_runs: dict[str, dict[str, Any]] = {}

PIPELINE_NODES = {
    "roster", "approval", "concepts", "process_item", "feedback",
    "script", "ltx", "kling", "seedance",
    "product_demo", "qc", "assembly", "distribution", "drop",
}

ITEM_UPDATE_NODES = {
    "script", "ltx", "kling", "seedance",
    "product_demo", "qc", "assembly", "distribution", "drop",
    "process_item",
}

NODE_LABELS: dict[str, str] = {
    "roster": "Creator Roster",
    "approval": "Aceite Human",
    "concepts": "Conceitos",
    "process_item": "Item",
    "feedback": "Feedback",
    "script": "Script",
    "ltx": "Talking-Head (LTX)",
    "kling": "Talking-Head (Kling)",
    "seedance": "Talking-Head (Seedance)",
    "product_demo": "Product Demo",
    "qc": "QC",
    "assembly": "Montagem",
    "distribution": "Distribuição",
    "drop": "Descartado",
}


# --------------------------------------------------------------------------- #
# Emissão de eventos                                                           #
# --------------------------------------------------------------------------- #

def _emit_sync(run_id: str, event: dict[str, Any]) -> None:
    """Emite evento de forma síncrona (seguro dentro de contexto async)."""
    state = _runs.get(run_id)
    if state is None:
        return
    state["buffer"].append(event)
    for q in list(state["queues"]):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _emit(run_id: str, event: dict[str, Any]) -> None:
    _emit_sync(run_id, event)


def _to_plain(obj: Any) -> Any:
    """Converte pydantic models e containers para estruturas JSON-like."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


def _media_type_for_uri(uri: str) -> str:
    lower = uri.lower()
    if lower.startswith("data:image/"):
        return "image"
    if lower.startswith("data:video/"):
        return "video"
    if lower.startswith("data:audio/"):
        return "audio"
    path = urlparse(uri).path.lower()
    if path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")):
        return "image"
    if path.endswith((".mp4", ".mov", ".webm", ".m4v")):
        return "video"
    if path.endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return "audio"
    return "reference"


def _is_renderable_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https"}:
        return _media_type_for_uri(uri) != "reference"
    if uri.startswith("data:"):
        return _media_type_for_uri(uri) in {"image", "video", "audio"}
    if parsed.scheme:
        return False
    # Path local já servido pelo web app, absoluto ou relativo.
    if uri.startswith("/") or uri.startswith("./") or uri.startswith("../"):
        return _media_type_for_uri(uri) != "reference"
    return False


def _normalize_artifact(art: Any) -> Optional[dict[str, Any]]:
    """Normaliza um Artifact para o contrato público da UI."""
    art = _to_plain(art)
    if not isinstance(art, dict) or not art.get("uri"):
        return None
    uri = str(art["uri"])
    media_type = _media_type_for_uri(uri)
    return {
        "kind": art.get("kind", "artifact"),
        "uri": uri,
        "media_type": media_type,
        "renderable": _is_renderable_uri(uri),
    }


def _normalize_creator(creator: dict[str, Any]) -> dict[str, Any]:
    """Normaliza creator mantendo aliases legados durante a migração da UI."""
    image_uri = (
        creator.get("image_uri")
        or creator.get("image")
        or creator.get("upscaled_base")
    )
    voice_ref = (
        creator.get("voice_ref")
        or creator.get("voice")
        or creator.get("voice_id")
    )
    voice_preview_uri = (
        creator.get("voice_preview_uri")
        or creator.get("voice_preview")
        or creator.get("preview_uri")
    )
    return {
        "id": creator.get("id") or creator.get("creator_id"),
        "image_uri": image_uri,
        "voice_ref": voice_ref,
        "voice_preview_uri": voice_preview_uri,
        "image": image_uri,
        "voice": voice_ref,
        "angles": list(creator.get("angles") or []),
    }


def _pick_first_existing(directory: Path, names: tuple[str, ...]) -> Optional[Path]:
    for name in names:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _recover_creators_from_media(media_root: Path) -> list[dict[str, Any]]:
    """Reconstrói histórico básico quando o JSON de creators foi zerado."""
    if not media_root.exists():
        return []

    recovered: list[dict[str, Any]] = []
    image_names = ("image.png", "image.svg", "image.jpg", "image.jpeg", "image.webp")
    voice_names = ("voice.wav", "voice.mp3", "voice.m4a", "voice.ogg")

    for run_dir in sorted((p for p in media_root.iterdir() if p.is_dir()), reverse=True):
        creator_dirs = (
            p for p in run_dir.iterdir()
            if p.is_dir() and p.name.startswith("creator-")
        )
        for creator_dir in sorted(creator_dirs):
            image_path = _pick_first_existing(creator_dir, image_names)
            voice_path = _pick_first_existing(creator_dir, voice_names)
            if image_path is None and voice_path is None:
                continue
            image_uri = (
                f"/media/{run_dir.name}/{creator_dir.name}/{image_path.name}"
                if image_path else None
            )
            voice_uri = (
                f"/media/{run_dir.name}/{creator_dir.name}/{voice_path.name}"
                if voice_path else None
            )
            recovered.append({
                "run_id": run_dir.name,
                "creator_id": creator_dir.name,
                "id": creator_dir.name,
                "image_uri": image_uri,
                "image": image_uri,
                "voice_ref": voice_uri,
                "voice": voice_uri,
                "voice_preview_uri": voice_uri,
                "angles": [],
                "creator_prompt": None,
                "video_prompt": None,
                "offer": None,
                "status": "recovered",
            })

    return recovered


def _artifact_dict(art: Any) -> Optional[dict[str, Any]]:
    """Normaliza um Artifact (model ou dict) para o contrato público da UI."""
    if hasattr(art, "model_dump"):
        art = art.model_dump()
    return _normalize_artifact(art)


def _extract_artifacts(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Lista os artefatos gerados (clips + montagem final) com kind e uri."""
    arts: list[dict[str, Any]] = []
    for clip in item.get("clips", []) or []:
        norm = _artifact_dict(clip)
        if norm:
            arts.append(norm)
    final = _artifact_dict(item.get("assembled"))
    if final:
        arts.append(final)
    return arts


def _normalize_qc(qc: Any) -> Optional[dict[str, Any]]:
    qc = _to_plain(qc)
    if not isinstance(qc, dict):
        return None
    return {
        "passed": bool(qc.get("passed")),
        "score": qc.get("score"),
        "reasons": list(qc.get("reasons") or []),
    }


def _snapshot_from_item(item: Any) -> dict[str, Any]:
    item = _to_plain(item)
    if not isinstance(item, dict):
        return {}
    snap: dict[str, Any] = {}
    for key in (
        "id", "creator_ref", "concept", "script", "tier",
        "attempts", "cost_usd", "distributed", "dropped",
    ):
        if key in item:
            snap[key] = _safe_serialize(item[key])
    if item.get("qc") is not None:
        snap["qc"] = _normalize_qc(item["qc"])
    artifacts = _extract_artifacts(item)
    if artifacts:
        snap["artifacts"] = artifacts
    assembled = _normalize_artifact(item.get("assembled"))
    if assembled:
        snap["assembled"] = assembled
    return snap


def _merge_artifacts(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for art in (existing or []) + (incoming or []):
        key = (str(art.get("kind")), str(art.get("uri")))
        if art.get("uri") and key not in seen:
            merged.append(art)
            seen.add(key)
    return merged


def _merge_item_snapshot(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = {**base, **{k: v for k, v in incoming.items() if k != "artifacts"}}
    if "artifacts" in base or "artifacts" in incoming:
        merged["artifacts"] = _merge_artifacts(base.get("artifacts"), incoming.get("artifacts"))
    return merged


def _item_id_from(data: dict[str, Any], current: dict[str, Any]) -> Optional[str]:
    for candidate in (data.get("input"), data.get("output"), current):
        plain = _to_plain(candidate)
        if isinstance(plain, dict) and plain.get("id"):
            return str(plain["id"])
    output = _to_plain(data.get("output"))
    if isinstance(output, dict):
        results = output.get("results") or []
        if results:
            item = _to_plain(results[-1])
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
    return None


def _complete_item_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot.get("id"),
        "creator_ref": snapshot.get("creator_ref"),
        "concept": snapshot.get("concept") or {},
        "script": snapshot.get("script"),
        "tier": snapshot.get("tier"),
        "attempts": snapshot.get("attempts", 0),
        "cost_usd": snapshot.get("cost_usd", 0.0),
        "qc": snapshot.get("qc"),
        "artifacts": snapshot.get("artifacts") or [],
        "assembled": snapshot.get("assembled"),
        "distributed": snapshot.get("distributed", False),
        "dropped": snapshot.get("dropped", False),
    }


def _build_item_update(
    run_id: str,
    node: str,
    data: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Cria ``item_update`` incremental a partir de um ``node_end`` LangGraph."""
    if node not in ITEM_UPDATE_NODES:
        return None
    output = _to_plain(data.get("output"))
    if node == "process_item" and isinstance(output, dict):
        results = output.get("results") or []
        if not results:
            return None
        incoming = _snapshot_from_item(results[-1])
    else:
        current = _snapshot_from_item(data.get("input"))
        item_id = _item_id_from(data, current)
        if not item_id:
            return None
        incoming = _merge_item_snapshot(current, _snapshot_from_item(output))
        incoming["id"] = item_id

    item_id = str(incoming.get("id") or "")
    if not item_id:
        return None
    previous = snapshots.get(item_id, {})
    snapshot = _merge_item_snapshot(previous, incoming)
    snapshots[item_id] = snapshot
    return {
        "type": "item_update",
        "run_id": run_id,
        "node": node,
        "label": NODE_LABELS.get(node, node),
        "item": _safe_serialize(_complete_item_payload(snapshot)),
    }


def _safe_serialize(obj: Any, depth: int = 0) -> Any:
    """Serializa de forma segura objetos do estado para JSON."""
    if depth > 3:
        return str(obj)
    if isinstance(obj, dict):
        return {k: _safe_serialize(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_serialize(i, depth + 1) for i in obj[:20]]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# --------------------------------------------------------------------------- #
# Background task de execução da pipeline                                     #
# --------------------------------------------------------------------------- #

async def _execute_run(
    run_id: str,
    offer: str,
    batch: int,
    platform: str,
    config_dir: Optional[str],
    db_path: str,
    creator_prompt: Optional[str] = None,
    video_prompt: Optional[str] = None,
) -> None:
    """Roda a pipeline completa, emitindo eventos para os subscribers SSE.

    Quando ``approve_creators=True`` o loop pausa no interrupt, emite
    ``awaiting_approval`` e aguarda a resolução do Future criado por
    ``POST /api/approve/{run_id}``, depois retoma com ``Command(resume=...)``.
    """

    def token_cb(event: dict[str, Any]) -> None:
        if event.get("type") == "creator_ready" and isinstance(event.get("creator"), dict):
            event = {**event, "creator": _normalize_creator(event["creator"])}
        _emit_sync(run_id, event)

    stream_bus.set_token_callback(token_cb)

    store_path = str(default_creator_store_path())
    # Guarda metadados do run para uso no record_creators
    run_state = _runs.get(run_id, {})
    run_state["offer"] = offer
    run_state["creator_prompt"] = creator_prompt
    run_state["video_prompt"] = video_prompt
    run_state.setdefault("item_snapshots", {})

    try:
        pipeline = load_pipeline(config_dir)
        providers = load_providers(config_dir)
        adapter = build_adapter_from_providers(providers, pipeline)

        cfg: dict[str, Any] = {
            "configurable": {
                "adapter": adapter,
                "pipeline": pipeline,
                "run": {
                    "platform": platform,
                    "creator_prompt": creator_prompt,
                    "video_prompt": video_prompt,
                    "approve_creators": True,
                },
                "thread_id": run_id,
            },
            "max_concurrency": int(pipeline.get("batch", {}).get("max_concurrency", 8)),
            "recursion_limit": 100,
        }
        cfg.update(run_trace_config(run_id, offer=offer, platform=platform, batch=batch))
        init: Any = {
            "run_id": run_id,
            "config": {"offer": offer, "batch_size": batch},
        }

        await _emit(run_id, {"type": "run_start", "run_id": run_id, "offer": offer, "batch": batch})

        final_output: dict[str, Any] = {}

        async with open_checkpointer(db_path) as cp:
            graph = build_graph(pipeline, checkpointer=cp)
            resume_input = init

            while True:
                async for event in graph.astream_events(resume_input, cfg, version="v2"):
                    etype: str = event["event"]
                    meta = event.get("metadata", {})
                    node = meta.get("langgraph_node") or event.get("name", "")

                    if node in PIPELINE_NODES:
                        if etype == "on_chain_start":
                            await _emit(run_id, {
                                "type": "node_start",
                                "node": node,
                                "label": NODE_LABELS.get(node, node),
                            })
                        elif etype == "on_chain_end":
                            data = event.get("data", {})
                            output = data.get("output", {})
                            payload: dict[str, Any] = {
                                "type": "node_end",
                                "node": node,
                                "label": NODE_LABELS.get(node, node),
                            }
                            # Para process_item extraímos o resumo do item
                            if node == "process_item" and isinstance(output, dict):
                                items = output.get("results", [])
                                if items:
                                    item = items[-1]
                                    if hasattr(item, "model_dump"):
                                        item = item.model_dump()
                                    payload["item"] = _safe_serialize({
                                        "id": item.get("id"),
                                        "concept": item.get("concept", {}),
                                        "distributed": item.get("distributed"),
                                        "dropped": item.get("dropped"),
                                        "attempts": item.get("attempts"),
                                        "cost_usd": item.get("cost_usd"),
                                        "qc": item.get("qc"),
                                        "artifacts": _extract_artifacts(item),
                                    })
                            await _emit(run_id, payload)
                            item_update = _build_item_update(
                                run_id,
                                node,
                                data,
                                run_state.setdefault("item_snapshots", {}),
                            )
                            if item_update:
                                await _emit(run_id, item_update)

                    # Captura o estado final do grafo raiz
                    if etype == "on_chain_end" and event.get("name") == "LangGraph":
                        out = event.get("data", {}).get("output", {})
                        if isinstance(out, dict):
                            final_output = out

                # Verifica se há interrupt pendente
                snap = await graph.aget_state(cfg)
                all_interrupts = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
                if snap.next and all_interrupts:
                    intr_payload = all_interrupts[0].value  # {"type":"approve_creators",...}
                    # NÃO usar **intr_payload aqui: ele carrega seu próprio "type"
                    # ("approve_creators") que sobrescreveria o "awaiting_approval".
                    await _emit(run_id, {
                        "type": "awaiting_approval",
                        "creators": [
                            _normalize_creator(c)
                            for c in intr_payload.get("creators", [])
                        ],
                    })
                    # Cria Future e aguarda decisão via POST /api/approve
                    fut: asyncio.Future = asyncio.get_event_loop().create_future()
                    run_state_ref = _runs.get(run_id)
                    if run_state_ref is not None:
                        run_state_ref["approval"] = fut
                    decision = await fut
                    # Persiste creators no store
                    creator_store.record_creators(
                        store_path, run_id,
                        [_normalize_creator(c) for c in intr_payload.get("creators", [])],
                        approved_ids=decision.get("approved", []),
                        creator_prompt=creator_prompt,
                        video_prompt=video_prompt,
                        offer=offer,
                    )
                    resume_input = Command(resume=decision)
                    continue
                break

        summary = runner.summarize({**final_output, "run_id": run_id}) if final_output else {}
        await _emit(run_id, {"type": "run_end", "run_id": run_id, "summary": summary})

    except Exception as exc:  # noqa: BLE001
        await _emit(run_id, {"type": "error", "message": str(exc)})

    finally:
        stream_bus.clear_token_callback()
        state = _runs.get(run_id)
        if state:
            state["done"] = True
            for q in list(state["queues"]):
                q.put_nowait(None)  # sentinel: fecha o stream SSE


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #

class RunRequest(BaseModel):
    offer: str = "demo offer"
    batch: int = 6
    platform: str = "tiktok"
    config_dir: Optional[str] = None
    db: Optional[str] = None
    creator_prompt: Optional[str] = None
    video_prompt: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/api/run")
async def start_run(req: RunRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    run_id = f"web-{uuid.uuid4().hex[:8]}"
    _runs[run_id] = {"queues": [], "buffer": [], "done": False}
    db_path = req.db or str(default_db_path())
    background_tasks.add_task(
        _execute_run,
        run_id, req.offer, req.batch, req.platform, req.config_dir, db_path,
        req.creator_prompt, req.video_prompt,
    )
    return {"run_id": run_id}


class ApproveRequest(BaseModel):
    approved: list[str] = []


@app.post("/api/approve/{run_id}")
async def approve(run_id: str, req: ApproveRequest) -> dict[str, Any]:
    st = _runs.get(run_id)
    fut = (st or {}).get("approval")
    if not fut or fut.done():
        raise HTTPException(409, "nenhuma aprovação pendente")
    fut.set_result({"approved": req.approved})
    return {"ok": True}


@app.get("/api/creators")
async def creators_history() -> dict[str, Any]:
    store_path = default_creator_store_path()
    creators = creator_store.load_creators(str(store_path))
    if not creators:
        creators = _recover_creators_from_media(default_media_path())
    return {
        "creators": creators,
        "store_path": str(store_path),
        "exists": store_path.exists(),
    }


@app.get("/api/stream/{run_id}")
async def stream_events(run_id: str) -> StreamingResponse:
    state = _runs.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    q: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=500)

    # Replay eventos já emitidos (para clientes que conectam tarde)
    for event in state["buffer"]:
        q.put_nowait(event)

    if state["done"]:
        q.put_nowait(None)
    else:
        state["queues"].append(q)

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    yield "data: {\"type\": \"stream_end\"}\n\n"
                    return
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            qs = _runs.get(run_id, {}).get("queues", [])
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


@app.get("/api/runs")
async def list_runs_endpoint(db: Optional[str] = None) -> dict[str, Any]:
    db_path = db or str(default_db_path())
    return {"runs": runner.list_runs(db_path), "active": list(_runs.keys())}


@app.get("/api/status/{run_id}")
async def run_status(run_id: str, config_dir: Optional[str] = None, db: Optional[str] = None) -> Any:
    pipeline = load_pipeline(config_dir)
    db_path = db or str(default_db_path())
    state = await runner.get_status(pipeline, db_path=db_path, run_id=run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return runner.summarize({**state, "run_id": run_id})
