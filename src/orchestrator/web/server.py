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
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from langgraph.types import Command

from orchestrator import runner, stream_bus
from orchestrator.auth import CloudflareAccessMiddleware
import orchestrator.creator_store as creator_store
import orchestrator.job_store as job_store
import orchestrator.prompt_store as prompt_store
import orchestrator.run_store as run_store
from orchestrator.config import (
    default_creator_store_path,
    default_db_path,
    default_media_path,
    default_prompt_store_path,
    default_videos_path,
    load_agent_catalog,
    load_judge,
    load_pipeline,
    load_providers,
)
from orchestrator.db import Database, close_shared_database, get_shared_database
from orchestrator.tracing import run_trace_config
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.nodes.stages import reroll_creator_voice as reroll_creator_voice_in_stage
from orchestrator.registry import build_adapter_from_providers
from orchestrator.storage.factory import build_media_storage
from orchestrator.storage.r2 import R2MediaStorage
from orchestrator.storage.resolve import resolve_signed_uris

@asynccontextmanager
async def _app_lifespan(app_: FastAPI):
    database: Database | None = None
    if os.environ.get("ORCH_AUTH_MODE", "disabled") == "cloudflare_access":
        database = Database.from_env()
        await database.open()
        app_.state.auth_database = database
    elif os.environ.get("DATABASE_URL"):
        database = await get_shared_database()
    try:
        yield
    finally:
        if database is not None:
            await database.close()
        await close_shared_database()



app = FastAPI(title="UGC Orchestrator", lifespan=_app_lifespan)
app.add_middleware(CloudflareAccessMiddleware)


def _cors_origins_from_env() -> list[str]:
    raw = os.environ.get("ORCH_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


_cors_origins = _cors_origins_from_env()


def _install_cors(app_: FastAPI, origins: list[str]) -> None:
    if not origins:
        return
    app_.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_install_cors(app, _cors_origins)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: o processo respondeu. Não toca config nem IO externo."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: a config carrega e o backend de storage é resolvível.

    Não chama provider pago nem faz request S3 — só valida config e credenciais.
    """
    try:
        load_pipeline()
        providers = load_providers()
        load_judge()
        backend = ((providers or {}).get("storage") or {}).get("backend", "local")
        if backend == "local":
            pass
        elif backend == "r2":
            R2MediaStorage.from_env()  # valida credenciais R2; não faz request de rede
        else:
            raise ValueError(f"unknown storage backend {backend!r}")
    except Exception as exc:  # readiness: qualquer erro de config = not ready
        return JSONResponse(status_code=503, content={"status": "not-ready", "reason": str(exc)})
    return JSONResponse(status_code=200, content={"status": "ready", "storage": backend})

# Front-end SPA ("Kinetic Command", Vite+React) built into front/dist. Repo layout:
#   <repo>/front/dist/            ← this file is <repo>/src/orchestrator/web/server.py
_FRONT_DIST = Path(__file__).resolve().parents[3] / "front" / "dist"


def _front_index() -> Optional[Path]:
    idx = _FRONT_DIST / "index.html"
    return idx if idx.exists() else None


_UNBUILT_FALLBACK = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Orchestrator AI</title></head><body style=\"font-family:system-ui;"
    "max-width:640px;margin:80px auto;padding:0 24px;color:#191c1e\">"
    "<h1>Front-end not built</h1><p>The React UI lives in <code>front/</code>. "
    "Build it once, then reload:</p>"
    "<pre style=\"background:#f2f4f6;padding:16px;border-radius:8px\">"
    "cd front\nnpm install\nnpm run build</pre>"
    "<p>During development you can instead run <code>npm run dev</code> "
    "(Vite proxies the API to this server).</p></body></html>"
)

# Serve os bytes persistidos do creator (imagem/voz baixadas pelo media_store) em
# /media/{run_id}/{creator_id}/...; _is_renderable_uri já trata esses paths.
_media_root = default_media_path()
_media_root.mkdir(parents=True, exist_ok=True)
_videos_root = default_videos_path()
_videos_root.mkdir(parents=True, exist_ok=True)


def _serve_local_media_enabled() -> bool:
    """Se o FastAPI deve servir /media e /videos do disco local.

    Em produção com storage R2 o browser recebe URLs assinadas (D30), então o disco
    local não precisa ser montado — ADR-D36 exige disco só como temporário. Default
    ligado para preservar o comportamento local/dev.
    """
    return os.environ.get("ORCH_SERVE_LOCAL_MEDIA", "1").strip().lower() not in ("0", "false", "no", "")


def _install_media_mounts(app_: FastAPI) -> None:
    if not _serve_local_media_enabled():
        return
    app_.mount("/media", StaticFiles(directory=str(_media_root)), name="media")
    app_.mount("/videos", StaticFiles(directory=str(_videos_root)), name="videos")


_install_media_mounts(app)

# Hashed JS/CSS emitted by Vite (front/dist/assets). Mounted unconditionally with
# check_dir=False so import works in a Node-less CI/test env (unbuilt front); requests
# just 404 until `npm run build` populates the directory.
app.mount(
    "/assets",
    StaticFiles(directory=str(_FRONT_DIST / "assets"), check_dir=False),
    name="assets",
)

# run_id → {queues: list[Queue], buffer: list[dict], done: bool}
_runs: dict[str, dict[str, Any]] = {}
_RUN_REPOSITORY_UNSET = object()

PIPELINE_NODES = {
    "persona", "roster", "approval", "concepts", "scripts", "concept_review",
    "process_item", "feedback",
    "script", "ltx", "kling", "seedance",
    "product_demo", "qc", "assembly", "upscale", "drop",
}

ITEM_UPDATE_NODES = {
    "script", "ltx", "kling", "seedance",
    "product_demo", "qc", "assembly", "upscale", "drop",
    "process_item",
}

NODE_LABELS: dict[str, str] = {
    "persona": "Persona",
    "roster": "Creator Roster",
    "approval": "Aceite Human",
    "concepts": "Conceitos",
    "scripts": "Scripts",
    "concept_review": "Edição de Conceitos",
    "process_item": "Item",
    "feedback": "Feedback",
    "script": "Script",
    "ltx": "Talking-Head (LTX)",
    "kling": "Talking-Head (Kling)",
    "seedance": "Talking-Head (Seedance)",
    "product_demo": "Product Demo",
    "qc": "QC",
    "assembly": "Montagem",
    "upscale": "Upscale (vídeo)",
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


def _signing_storage(config_dir: Optional[str]) -> Optional[Any]:
    """Backend de storage quando ele assina URLs; ``None`` quando não precisa.

    O backend local serve ``/media`` e ``/videos`` direto do disco — não há o que
    assinar. Config de storage quebrada devolve ``None`` em vez de propagar: um
    ``providers.yaml`` inválido já derruba o *run* no boot (falha alto, D30), mas não
    pode cegar o dashboard inteiro, que é justamente onde o operador vai ler o erro.
    """
    try:
        storage = build_media_storage(
            load_providers(config_dir), root=_media_root, web_prefix="/media",
        )
    except Exception:  # noqa: BLE001 — dashboard nunca cai por config de storage
        return None
    return storage if getattr(storage, "backend", "local") != "local" else None


async def _sign_payload(payload: Any, config_dir: Optional[str]) -> Any:
    """Troca ponteiros ``r2://`` por signed URLs de TTL curto, só na saída (D30).

    Nunca persiste o resultado: a verdade a montante segue sendo o ``storage_key``.
    """
    return await resolve_signed_uris(payload, storage=_signing_storage(config_dir))


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
    # Ponteiro canônico do R2 (D30): vira signed URL https na saída (``_sign_payload``),
    # então a UI consegue tocá-lo. Outros schemes seguem sendo referência opaca.
    if parsed.scheme == "r2":
        return _media_type_for_uri(uri) != "reference"
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


def _normalize_creator_history(creator: dict[str, Any]) -> dict[str, Any]:
    """Normaliza creator do histórico sem perder metadados salvos no store."""
    return {**creator, **_normalize_creator(creator)}


def _playable_voice_uri(creator: dict[str, Any]) -> Optional[str]:
    """URI de voz que o browser consegue tocar (path local /media, http(s) ou data:audio).

    Refs opacas (``voice_id`` do ElevenLabs, ``voice-0`` do mock) não tocam na web.
    """
    for key in ("voice_preview_uri", "voice_preview", "preview_uri", "voice_ref", "voice", "voice_id"):
        uri = creator.get(key)
        if (
            isinstance(uri, str)
            and uri
            and _is_renderable_uri(uri)
            and _media_type_for_uri(uri) == "audio"
        ):
            return uri
    return None


def _has_complete_media(creator: dict[str, Any]) -> bool:
    """True só para uma pessoa completa: imagem renderizável + voz tocável.

    Entradas que só têm prompt/metadata (a "inspiração") ficam fora da galeria.
    """
    image = (
        creator.get("image_uri")
        or creator.get("image")
        or creator.get("upscaled_base")
    )
    has_image = (
        isinstance(image, str)
        and bool(image)
        and _is_renderable_uri(image)
        and _media_type_for_uri(image) == "image"
    )
    return has_image and _playable_voice_uri(creator) is not None


def _pending_creators_for(run_id: str) -> list[dict[str, Any]]:
    state = _runs.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    creators = state.get("pending_creators")
    if not isinstance(creators, list) or not creators:
        raise HTTPException(status_code=409, detail="nenhum creator pendente para aprovação")
    return creators


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
            # Só pessoas completas entram na galeria: imagem E voz em disco.
            if image_path is None or voice_path is None:
                continue
            image_uri = f"/media/{run_dir.name}/{creator_dir.name}/{image_path.name}"
            voice_uri = f"/media/{run_dir.name}/{creator_dir.name}/{voice_path.name}"
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


async def _find_creator_for_draft_repository(
    creator_id: str,
    creator_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve no backend ativo e só cai para varredura local quando necessário."""
    async with creator_store.open_repository(default_creator_store_path()) as repository:
        creator = await repository.find_creator(creator_id, creator_run_id)
    if creator is not None:
        return _normalize_creator(creator)

    for recovered in _recover_creators_from_media(default_media_path()):
        if _creator_id(recovered) != creator_id:
            continue
        if creator_run_id is not None and str(recovered.get("run_id") or "") != creator_run_id:
            continue
        return _normalize_creator(recovered)

    detail = f"creator {creator_id!r} not found"
    if creator_run_id is not None:
        detail = f"creator {creator_id!r} not found for run {creator_run_id!r}"
    raise HTTPException(status_code=404, detail=detail)


def _creator_id(creator: dict[str, Any]) -> Optional[str]:
    raw = creator.get("id") or creator.get("creator_id")
    return str(raw) if raw is not None else None


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
        "attempts", "cost_usd", "dropped", "error",
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
        "dropped": snapshot.get("dropped", False),
        "error": snapshot.get("error"),
    }


def _item_payload_from_result(item: Any) -> dict[str, Any]:
    return _safe_serialize(_complete_item_payload(_snapshot_from_item(item)))


def _runtime_phase(
    state: dict[str, Any] | None, summary: dict[str, Any] | None,
) -> str:
    if state is not None:
        # Falha de run inteiro vence tudo: o `finally` marca done=True mesmo num
        # crash, então o erro precisa ser checado antes para não virar "done".
        if state.get("error"):
            return "error"
        concept_future = state.get("concept_edit")
        if concept_future is not None and not getattr(concept_future, "done", lambda: False)():
            return "editing"
        approval_future = state.get("approval")
        if approval_future is not None and not getattr(approval_future, "done", lambda: False)():
            return "awaiting"
        if state.get("done"):
            return "done"
        return "running"
    if summary is None:
        return "idle"
    if int(summary.get("in_flight") or 0) > 0:
        return "running"
    return "done"


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
    approve_creators: bool = True,
    edit_concepts: bool = True,
    seed_creator: Optional[dict[str, Any]] = None,
    _run_repository: Any = _RUN_REPOSITORY_UNSET,
) -> None:
    """Roda a pipeline completa, emitindo eventos para os subscribers SSE.

    Quando ``approve_creators=True`` o loop pausa no interrupt, emite
    ``awaiting_approval`` e aguarda a resolução do Future criado por
    ``POST /api/approve/{run_id}``, depois retoma com ``Command(resume=...)``.
    """
    if _run_repository is _RUN_REPOSITORY_UNSET:
        async with run_store.open_repository() as repository:
            await _execute_run(
                run_id,
                offer,
                batch,
                platform,
                config_dir,
                db_path,
                creator_prompt,
                video_prompt,
                approve_creators,
                edit_concepts,
                seed_creator,
                repository,
            )
        return

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
        agent_catalog = load_agent_catalog(config_dir)
        adapter = build_adapter_from_providers(providers, pipeline)
        run_state["adapter"] = adapter

        cfg: dict[str, Any] = {
            "configurable": {
                "adapter": adapter,
                "pipeline": pipeline,
                "agent_catalog": agent_catalog,
                "run": {
                    "platform": platform,
                    "creator_prompt": creator_prompt,
                    "video_prompt": video_prompt,
                    # Default: pausa no gate humano para o usuário escolher quais
                    # creators (imagem + voz) estrelam os vídeos; opt-out via
                    # approve_creators=False no POST /api/run.
                    "approve_creators": approve_creators,
                    # Default: pausa ANTES do creator para o usuário editar/descartar
                    # concept+script; opt-out via edit_concepts=False no POST /api/run.
                    "edit_concepts": edit_concepts,
                    "seed_creator": seed_creator,
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
                                persisted_items = [
                                    _safe_serialize(_complete_item_payload(snapshot))
                                    for snapshot in run_state["item_snapshots"].values()
                                    if isinstance(snapshot, dict)
                                ]
                                if _run_repository is not None:
                                    await _run_repository.save(
                                        run_id,
                                        phase="running",
                                        state={
                                            "run_id": run_id,
                                            "offer": offer,
                                            "platform": platform,
                                        },
                                        summary=runner.summarize({
                                            "run_id": run_id,
                                            "results": persisted_items,
                                        }),
                                        items=persisted_items,
                                    )
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
                    intr_payload = all_interrupts[0].value  # {"type": ...}
                    # Gate de edição de concept+script (ANTES do creator).
                    if intr_payload.get("type") == "edit_concepts":
                        concepts = [
                            _safe_serialize(c) for c in intr_payload.get("concepts", [])
                        ]
                        await _emit(run_id, {
                            "type": "awaiting_concept_edit",
                            "run_id": run_id,
                            "concepts": concepts,
                        })
                        cfut: asyncio.Future = asyncio.get_event_loop().create_future()
                        run_state_ref = _runs.get(run_id)
                        if run_state_ref is not None:
                            run_state_ref["concept_edit"] = cfut
                            run_state_ref["pending_concepts"] = concepts
                        persisted_state = _to_plain(dict(snap.values or {}))
                        persisted_state["pending_concepts"] = concepts
                        if _run_repository is not None:
                            await _run_repository.save(
                                run_id,
                                phase="editing",
                                state=persisted_state,
                                summary=runner.summarize({
                                    **dict(snap.values or {}),
                                    "run_id": run_id,
                                }),
                                items=[],
                            )
                        cdecision = await cfut
                        resume_input = Command(resume=cdecision)
                        continue
                    # NÃO usar **intr_payload aqui: ele carrega seu próprio "type"
                    # ("approve_creators") que sobrescreveria o "awaiting_approval".
                    pending_creators = [
                        _normalize_creator(c)
                        for c in intr_payload.get("creators", [])
                    ]
                    await _emit(run_id, {
                        "type": "awaiting_approval",
                        "creators": pending_creators,
                    })
                    # Cria Future e aguarda decisão via POST /api/approve
                    fut: asyncio.Future = asyncio.get_event_loop().create_future()
                    run_state_ref = _runs.get(run_id)
                    if run_state_ref is not None:
                        run_state_ref["approval"] = fut
                        run_state_ref["pending_creators"] = pending_creators
                    persisted_state = _to_plain(dict(snap.values or {}))
                    persisted_state["pending_creators"] = pending_creators
                    if _run_repository is not None:
                        await _run_repository.save(
                            run_id,
                            phase="awaiting",
                            state=persisted_state,
                            summary=runner.summarize({
                                **dict(snap.values or {}),
                                "run_id": run_id,
                            }),
                            items=[],
                        )
                    decision = await fut
                    # Persiste metadata e ponteiros canônicos; signed URLs nunca entram
                    # no repositório (D30).
                    async with creator_store.open_repository(store_path) as creators:
                        await creators.record_creators(
                            run_id,
                            decision.get("creators")
                            or [
                                _normalize_creator(c)
                                for c in intr_payload.get("creators", [])
                            ],
                            approved_ids=decision.get("approved", []),
                            creator_prompt=creator_prompt,
                            video_prompt=video_prompt,
                            offer=offer,
                        )
                    resume_input = Command(resume=decision)
                    continue
                # Em fluxos com subgrafo + interrupts, o último evento "LangGraph"
                # observado em astream_events pode ser um output intermediário. O
                # snapshot raiz é a fonte correta para o resumo público do run.
                if snap.values:
                    final_output = dict(snap.values)
                break

        summary = runner.summarize({**final_output, "run_id": run_id}) if final_output else {}
        if _run_repository is not None:
            await _run_repository.save(
                run_id,
                phase="done",
                state=_to_plain(final_output),
                summary=_safe_serialize(summary),
                items=[
                    _item_payload_from_result(item)
                    for item in (final_output.get("results") or [])
                ],
            )
        await _emit(run_id, {"type": "run_end", "run_id": run_id, "summary": summary})

    except Exception as exc:  # noqa: BLE001
        # Grava o erro no estado runtime além de emitir no SSE, para que a falha
        # persista (fase "error" + mensagem) em reconexões e na lista de campanhas,
        # não só no stream ao vivo.
        state = _runs.get(run_id)
        if state is not None:
            state["error"] = str(exc)
        snapshots = (state or {}).get("item_snapshots") or {}
        items = [
            _safe_serialize(_complete_item_payload(snapshot))
            for snapshot in snapshots.values()
            if isinstance(snapshot, dict)
        ] if isinstance(snapshots, dict) else []
        if _run_repository is not None:
            await _run_repository.save(
                run_id,
                phase="error",
                state={
                    "run_id": run_id,
                    "offer": offer,
                    "platform": platform,
                },
                summary={},
                items=items,
                error=str(exc),
            )
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
    approve_creators: bool = True
    edit_concepts: bool = True
    creator_id: Optional[str] = None
    creator_run_id: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve o SPA React (front/dist) ou um fallback quando ainda não foi buildado."""
    idx = _front_index()
    if idx is not None:
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse(_UNBUILT_FALLBACK)


@app.post("/api/run")
async def start_run(req: RunRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    seed_creator = None
    if req.creator_id:
        seed_creator = await _find_creator_for_draft_repository(
            req.creator_id,
            req.creator_run_id,
        )
    run_id = f"web-{uuid.uuid4().hex[:8]}"
    db_path = req.db or str(default_db_path())
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
                    "config_dir": req.config_dir,
                    "db_path": db_path,
                    "creator_prompt": req.creator_prompt,
                    "video_prompt": req.video_prompt,
                    "approve_creators": req.approve_creators,
                    "edit_concepts": req.edit_concepts,
                    "seed_creator": seed_creator,
                },
            )
            return {"run_id": run_id, "job_id": str(queued.job_id)}

    # No caminho local, API e executor vivem no mesmo processo. No caminho durável,
    # o job acima guarda só o ponteiro canônico e o Runner assina no consumo.
    if seed_creator is not None:
        seed_creator = await _sign_payload(seed_creator, req.config_dir)
    _runs[run_id] = {"queues": [], "buffer": [], "done": False}
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
        run_id, req.offer, req.batch, req.platform, req.config_dir, db_path,
        req.creator_prompt, req.video_prompt,
        req.approve_creators, req.edit_concepts, seed_creator,
    )
    return {"run_id": run_id}


class ApproveRequest(BaseModel):
    approved: list[str] = []
    gate_id: Optional[str] = None
    version: Optional[int] = None


@app.post("/api/approve/{run_id}/creators/{creator_id}/reroll-voice")
async def reroll_creator_voice(run_id: str, creator_id: str) -> dict[str, Any]:
    state = _runs.get(run_id)
    creators = _pending_creators_for(run_id)
    adapter = (state or {}).get("adapter")
    if adapter is None:
        raise HTTPException(status_code=409, detail="adapter indisponível para reroll")

    for index, creator in enumerate(creators):
        if creator.get("id") != creator_id:
            continue
        updated = _normalize_creator(
            await reroll_creator_voice_in_stage(
                adapter,
                creator,
                run_id=run_id,
                media_root=default_media_path(),
            )
        )
        creators[index] = updated
        await _emit(run_id, {"type": "creator_update", "run_id": run_id, "creator": updated})
        return {"ok": True, "creator": updated}

    raise HTTPException(status_code=404, detail=f"creator {creator_id!r} not found")


@app.post("/api/approve/{run_id}")
async def approve(run_id: str, req: ApproveRequest) -> dict[str, Any]:
    if os.environ.get("DATABASE_URL"):
        if req.gate_id is None or req.version is None:
            raise HTTPException(409, "gate_id e version são obrigatórios")
        from orchestrator.db import StaleGateError

        async with job_store.open_repository() as jobs:
            assert jobs is not None
            try:
                resume = await jobs.resolve_gate(
                    uuid.UUID(req.gate_id),
                    version=req.version,
                    resolution={"approved": req.approved},
                )
            except (ValueError, StaleGateError) as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "job_id": str(resume.job_id)}
    st = _runs.get(run_id)
    fut = (st or {}).get("approval")
    if not fut or fut.done():
        raise HTTPException(409, "nenhuma aprovação pendente")
    fut.set_result({
        "approved": req.approved,
        "creators": list((st or {}).get("pending_creators") or []),
    })
    return {"ok": True}


class ConceptEditRequest(BaseModel):
    # Conceitos editados e INCLUÍDOS (os excluídos simplesmente não vêm na lista).
    # Cada item é o dict do conceito com o campo "script" já editado.
    concepts: list[dict[str, Any]] = []
    gate_id: Optional[str] = None
    version: Optional[int] = None


@app.post("/api/approve/{run_id}/concepts")
async def submit_concepts(run_id: str, req: ConceptEditRequest) -> dict[str, Any]:
    if os.environ.get("DATABASE_URL"):
        if req.gate_id is None or req.version is None:
            raise HTTPException(409, "gate_id e version são obrigatórios")
        from orchestrator.db import StaleGateError

        async with job_store.open_repository() as jobs:
            assert jobs is not None
            try:
                resume = await jobs.resolve_gate(
                    uuid.UUID(req.gate_id),
                    version=req.version,
                    resolution={"concepts": req.concepts},
                )
            except (ValueError, StaleGateError) as exc:
                raise HTTPException(409, str(exc)) from exc
        return {
            "ok": True,
            "count": len(req.concepts),
            "job_id": str(resume.job_id),
        }
    st = _runs.get(run_id)
    fut = (st or {}).get("concept_edit")
    if not fut or fut.done():
        raise HTTPException(409, "nenhuma edição de conceitos pendente")
    fut.set_result({"concepts": req.concepts})
    return {"ok": True, "count": len(req.concepts)}


class PromptTemplateRequest(BaseModel):
    kind: str
    title: str
    text: str
    desc: str = ""


@app.get("/api/prompts")
async def prompts_index() -> dict[str, Any]:
    """Templates salvos + último prompt usado, em PostgreSQL ou JSON local."""
    store_path = default_prompt_store_path()
    async with prompt_store.open_repository(store_path) as prompts:
        return {
            "templates": await prompts.list_templates(),
            "last_used": await prompts.get_last_used(),
            "store_path": prompts.location,
            "exists": prompts.exists,
        }


@app.post("/api/prompts")
async def save_prompt_template(req: PromptTemplateRequest) -> dict[str, Any]:
    try:
        async with prompt_store.open_repository(default_prompt_store_path()) as prompts:
            saved = await prompts.save_template(
                kind=req.kind, title=req.title, text=req.text, desc=req.desc,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "template": saved}


@app.delete("/api/prompts/{template_id}")
async def delete_prompt_template(template_id: str) -> dict[str, Any]:
    async with prompt_store.open_repository(default_prompt_store_path()) as prompts:
        if not await prompts.delete_template(template_id):
            raise HTTPException(status_code=404, detail=f"template {template_id!r} not found")
    return {"ok": True}


@app.get("/api/creators")
async def creators_history() -> dict[str, Any]:
    store_path = default_creator_store_path()
    async with creator_store.open_repository(store_path) as repository:
        creators = [
            _normalize_creator_history(c)
            for c in await repository.load_creators()
            if _has_complete_media(c)
        ]
        location = repository.location
        exists = repository.exists
    if not creators:
        creators = [
            _normalize_creator_history(c)
            for c in _recover_creators_from_media(default_media_path())
        ]
    return await _sign_payload(
        {
            "creators": creators,
            "store_path": location,
            "exists": exists,
        },
        None,
    )


@app.get("/api/integrations")
async def integrations_index(config_dir: Optional[str] = None) -> dict[str, Any]:
    """Mapa stage → adapter lido de providers.yaml (fonte da tela Integrations Hub)."""
    providers = load_providers(config_dir)
    agent_catalog = load_agent_catalog(config_dir)
    adapters = (providers or {}).get("adapters", {}) or {}
    stages = {str(k): str(v) for k, v in adapters.items()}
    return {"stages": stages, "agents": agent_catalog.as_dict()}


@app.get("/api/stream/{run_id}")
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
        storage = _signing_storage(config_dir)

        async def generate_persisted():
            cursor = after_seq
            while True:
                async with job_store.open_repository() as jobs:
                    assert jobs is not None
                    events = await jobs.list_events(run_id, after_seq=cursor)
                for event in events:
                    cursor = event.seq
                    payload = await resolve_signed_uris(
                        {**event.data, "type": event.event_type},
                        storage=storage,
                    )
                    yield (
                        f"id: {event.seq}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
                async with run_store.open_repository() as runs:
                    snapshot = await runs.get(run_id) if runs is not None else None
                if snapshot is not None and snapshot.phase in {"done", "error"}:
                    yield 'data: {"type": "stream_end"}\n\n'
                    return
                await asyncio.sleep(1)

        return StreamingResponse(
            generate_persisted(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

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

    # Uma vez por stream, não por evento: cada chamada reconstrói o client boto3, e um
    # stream longo emite centenas de eventos. O TTL da URL só começa a correr no yield,
    # então assinar aqui (e não no _emit) é o que mantém o buffer de replay com o
    # ponteiro canônico — URL assinada vence, ponteiro não.
    storage = _signing_storage(config_dir)

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
                event = await resolve_signed_uris(event, storage=storage)
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
    # `active` = só o que está realmente rodando; runs concluídos ou quebrados saem
    # daqui (senão a lista os rotularia "Generating" para sempre). `errored` deixa a
    # UI marcar os que falharam como "Failed".
    persisted_ids: list[str] = []
    persisted_errors: list[str] = []
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
                if entry.phase == "error" or entry.error
            ]
            persisted_active = [
                entry.run_id
                for entry in index
                if entry.phase in {"running", "editing", "awaiting"}
                and not entry.error
            ]
    if postgres_enabled:
        errored = persisted_errors
        active = persisted_active
        known = persisted_ids
    else:
        errored = [rid for rid, state in _runs.items() if state.get("error")]
        active = [
            rid
            for rid, state in _runs.items()
            if not state.get("error") and not state.get("done")
        ]
        known = runner.list_runs(db_path)
    return {"runs": known, "active": active, "errored": errored}


@app.get("/api/status/{run_id}")
async def run_status(run_id: str, config_dir: Optional[str] = None, db: Optional[str] = None) -> Any:
    pipeline = load_pipeline(config_dir)
    db_path = db or str(default_db_path())
    state = await runner.get_status(pipeline, db_path=db_path, run_id=run_id)
    if state is None:
        async with run_store.open_repository() as runs:
            persisted = await runs.get(run_id) if runs is not None else None
        if persisted is None:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
        return persisted.summary
    return runner.summarize({**state, "run_id": run_id})


@app.get("/api/state/{run_id}")
async def run_state(run_id: str, config_dir: Optional[str] = None, db: Optional[str] = None) -> dict[str, Any]:
    pipeline = load_pipeline(config_dir)
    db_path = db or str(default_db_path())
    checkpoint_state = await runner.get_status(pipeline, db_path=db_path, run_id=run_id)
    runtime_state = _runs.get(run_id)
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
        (runtime_state or {}).get("item_snapshots")
        if runtime_state is not None else None
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
        items = [
            _safe_serialize(_complete_item_payload(snapshots[item_id]))
            for item_id in order
        ]
    else:
        items = [_item_payload_from_result(item) for item in checkpoint_results]

    phase = (
        _runtime_phase(runtime_state, summary)
        if runtime_state is not None
        else persisted.phase if persisted is not None else _runtime_phase(None, summary)
    )
    edit_concepts: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    if runtime_state is not None and phase == "editing":
        edit_concepts = [
            _safe_serialize(c)
            for c in runtime_state.get("pending_concepts") or []
            if isinstance(c, dict)
        ]
    elif persisted is not None and phase == "editing":
        edit_concepts = [
            _safe_serialize(c)
            for c in persisted.state.get("pending_concepts") or []
            if isinstance(c, dict)
        ]
    if runtime_state is not None and phase == "awaiting":
        awaiting = [
            _normalize_creator(c)
            for c in runtime_state.get("pending_creators") or []
            if isinstance(c, dict)
        ]
    elif persisted is not None and phase == "awaiting":
        awaiting = [
            _normalize_creator(c)
            for c in persisted.state.get("pending_creators") or []
            if isinstance(c, dict)
        ]

    return await _sign_payload(
        {
            "run_id": run_id,
            "phase": phase,
            "items": items,
            "edit_concepts": edit_concepts,
            "awaiting": awaiting,
            "summary": summary,
            "error": (
                runtime_state.get("error")
                if runtime_state is not None
                else persisted.error if persisted is not None else None
            ),
        },
        config_dir,
    )


# --------------------------------------------------------------------------- #
# SPA fallback: rotas client-side (/campaigns, /analytics, …) devem servir o     #
# index do SPA para que refresh/deep-link funcionem. Registrado por último para #
# não sombrear /api, /media, /videos, /assets — esses continuam com seu 404/JSON.#
# --------------------------------------------------------------------------- #

_NON_SPA_PREFIXES = ("api/", "media/", "videos/", "assets/")


@app.get("/{full_path:path}", response_class=HTMLResponse)
async def spa_fallback(full_path: str) -> HTMLResponse:
    if full_path.startswith(_NON_SPA_PREFIXES):
        raise HTTPException(status_code=404, detail="not found")
    idx = _front_index()
    if idx is not None:
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse(_UNBUILT_FALLBACK)
