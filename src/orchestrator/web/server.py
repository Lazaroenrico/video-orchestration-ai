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

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from orchestrator import runner, stream_bus
from orchestrator.config import default_db_path, load_pipeline, load_providers
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.registry import build_adapter_from_providers

app = FastAPI(title="UGC Orchestrator")

# run_id → {queues: list[Queue], buffer: list[dict], done: bool}
_runs: dict[str, dict[str, Any]] = {}

PIPELINE_NODES = {
    "roster", "concepts", "process_item", "feedback",
    "script", "ltx", "kling", "seedance",
    "product_demo", "qc", "assembly", "distribution", "drop",
}

NODE_LABELS: dict[str, str] = {
    "roster": "Creator Roster",
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
) -> None:
    """Roda a pipeline completa, emitindo eventos para os subscribers SSE."""

    def token_cb(event: dict[str, Any]) -> None:
        _emit_sync(run_id, event)

    stream_bus.set_token_callback(token_cb)

    try:
        pipeline = load_pipeline(config_dir)
        providers = load_providers(config_dir)
        adapter = build_adapter_from_providers(providers, pipeline)

        cfg: dict[str, Any] = {
            "configurable": {
                "adapter": adapter,
                "pipeline": pipeline,
                "run": {"platform": platform},
                "thread_id": run_id,
            },
            "max_concurrency": int(pipeline.get("batch", {}).get("max_concurrency", 8)),
            "recursion_limit": 100,
        }
        init = {
            "run_id": run_id,
            "config": {"offer": offer, "batch_size": batch},
        }

        await _emit(run_id, {"type": "run_start", "run_id": run_id, "offer": offer, "batch": batch})

        final_output: dict[str, Any] = {}

        async with open_checkpointer(db_path) as cp:
            graph = build_graph(pipeline, checkpointer=cp)
            async for event in graph.astream_events(init, cfg, version="v2"):
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
                        output = event.get("data", {}).get("output", {})
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
                                })
                        await _emit(run_id, payload)

                # Captura o estado final do grafo raiz
                if etype == "on_chain_end" and event.get("name") == "LangGraph":
                    out = event.get("data", {}).get("output", {})
                    if isinstance(out, dict):
                        final_output = out

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
    )
    return {"run_id": run_id}


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
