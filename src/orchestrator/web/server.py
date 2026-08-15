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
import logging
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field, model_validator

import orchestrator.creator_store as creator_store
import orchestrator.job_store as job_store
import orchestrator.prompt_store as prompt_store
import orchestrator.run_store as run_store
from orchestrator import runner, stream_bus
from orchestrator.auth import CloudflareAccessMiddleware
from orchestrator.config import (
    default_creator_store_path,
    default_db_path,
    default_media_path,
    default_prompt_store_path,
    default_videos_path,
    load_agent_catalog,
    load_pipeline,
    load_providers,
)
from orchestrator.creative_contracts import CampaignInput
from orchestrator.creators import normalize_creator_payload
from orchestrator.db import (
    Database,
    PostgresEffectLedger,
    PostgresJobRepository,
    TenantIdentity,
    close_shared_database,
    get_shared_database,
)
from orchestrator.dependencies import RunDependencies
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.nodes.stages import (
    apply_review_concept_updates,
    apply_review_creator_updates,
    validate_voice_selections,
)
from orchestrator.progress import ProgressEventTranslator, build_activity, build_progress
from orchestrator.replicate_webhook import (
    ReplicateWebhookError,
    apply_replicate_event,
    decode_effect_ref,
    parse_and_verify_replicate_event,
)
from orchestrator.storage.factory import build_media_storage, resolve_storage_backend
from orchestrator.storage.r2 import R2MediaStorage
from orchestrator.storage.resolve import resolve_signed_uris
from orchestrator.tracing import run_trace_config
from orchestrator.worker import run_worker_once

_log = logging.getLogger(__name__)
_WEB_DEFAULT_CONFIG_DIR = "config-staging"
_web_runner_wake_event: asyncio.Event | None = None


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _effective_config_dir(config_dir: Optional[str]) -> str:
    return config_dir or os.environ.get("ORCH_CONFIG_DIR") or _WEB_DEFAULT_CONFIG_DIR


def _web_embedded_runner_enabled() -> bool:
    return _truthy_env("ORCH_WEB_EMBEDDED_RUNNER") and bool(os.environ.get("DATABASE_URL"))


def _web_runner_poll_interval() -> float:
    try:
        value = float(os.environ.get("ORCH_WEB_RUNNER_POLL_INTERVAL", "2"))
    except ValueError:
        return 2.0
    return value if value > 0 else 2.0


def _web_runner_worker_id() -> str:
    return os.environ.get("ORCH_WEB_RUNNER_WORKER_ID", "web-embedded-runner")


def _wake_web_embedded_runner() -> None:
    if _web_runner_wake_event is not None:
        _web_runner_wake_event.set()


async def _web_embedded_runner_loop(wake_event: asyncio.Event) -> None:
    worker_id = _web_runner_worker_id()
    database = await get_shared_database()
    tenant = await database.resolve_tenant(TenantIdentity.from_env())
    _log.info("web embedded runner iniciado: worker_id=%s", worker_id)
    try:
        while True:
            try:
                worked = await run_worker_once(
                    worker_id=worker_id,
                    database=database,
                    tenant=tenant,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - falha de job não pode derrubar a API
                _log.exception("web embedded runner falhou ao processar job")
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(
                    wake_event.wait(),
                    timeout=_web_runner_poll_interval(),
                )
                wake_event.clear()
            except asyncio.TimeoutError:
                pass
    finally:
        _log.info("web embedded runner encerrado: worker_id=%s", worker_id)


@asynccontextmanager
async def _app_lifespan(app_: FastAPI):
    global _web_runner_wake_event
    database: Database | None = None
    web_runner_task: asyncio.Task | None = None
    web_runner_event: asyncio.Event | None = None
    if os.environ.get("ORCH_AUTH_MODE", "disabled") == "cloudflare_access":
        database = Database.from_env()
        await database.open()
        app_.state.auth_database = database
    elif os.environ.get("DATABASE_URL"):
        database = await get_shared_database()
    if _web_embedded_runner_enabled():
        web_runner_event = asyncio.Event()
        _web_runner_wake_event = web_runner_event
        web_runner_task = asyncio.create_task(
            _web_embedded_runner_loop(web_runner_event),
            name="orchestrator-web-embedded-runner",
        )
    try:
        yield
    finally:
        if web_runner_task is not None:
            web_runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await web_runner_task
        if web_runner_event is not None and _web_runner_wake_event is web_runner_event:
            _web_runner_wake_event = None
        if database is not None:
            await database.close()
        await close_shared_database()



app = FastAPI(title="UGC Orchestrator", lifespan=_app_lifespan)
app.add_middleware(CloudflareAccessMiddleware)

_ORGANIZATION_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MAX_REPLICATE_WEBHOOK_BYTES = 1_000_000


async def _replicate_webhook_repositories(organization_slug: str):
    database = await get_shared_database()
    tenant = TenantIdentity(
        organization_slug,
        organization_slug,
        "replicate-webhook",
    ).context()
    return (
        PostgresEffectLedger(database, tenant),
        PostgresJobRepository(database, tenant),
    )


@app.post("/webhooks/replicate/{organization_slug}/{effect_ref}")
async def replicate_prediction_webhook(
    organization_slug: str,
    effect_ref: str,
    request: Request,
) -> dict[str, bool]:
    """Authenticate and reconcile a Replicate prediction without reopening runs."""
    signing_secret = os.environ.get("REPLICATE_WEBHOOK_SIGNING_SECRET", "").strip()
    correlation_secret = os.environ.get("ORCH_WEBHOOK_CORRELATION_SECRET", "").strip()
    if not signing_secret or not correlation_secret or not os.environ.get("DATABASE_URL"):
        raise HTTPException(status_code=503, detail="Replicate webhook is not configured")
    if not _ORGANIZATION_SLUG.fullmatch(organization_slug):
        raise HTTPException(status_code=404, detail="Webhook correlation not found")
    raw_body = await request.body()
    if len(raw_body) > _MAX_REPLICATE_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook body is too large")
    try:
        event = parse_and_verify_replicate_event(
            raw_body,
            request.headers,
            signing_secret=signing_secret,
        )
    except ReplicateWebhookError as exc:
        detail = str(exc)
        status_code = 401 if "signature" in detail or "timestamp" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    try:
        effect_key = decode_effect_ref(
            organization_slug,
            effect_ref,
            correlation_secret,
        )
    except ReplicateWebhookError as exc:
        raise HTTPException(status_code=404, detail="Webhook correlation not found") from exc

    ledger, events = await _replicate_webhook_repositories(organization_slug)
    try:
        changed = await apply_replicate_event(ledger, effect_key, event)
        effect = await ledger.get(effect_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Webhook effect not found") from exc
    if changed:
        await events.append_event(
            effect.run_id,
            "replicate_prediction_updated",
            {
                "effect_key": effect_key,
                "prediction_id": event.prediction_id,
                "provider_status": effect.provider_status,
            },
        )
    return {"ok": True, "changed": changed}

_DATABASE_UNAVAILABLE_ERRORS = (OperationalError, PoolTimeout)
_DATABASE_READY_ERRORS = _DATABASE_UNAVAILABLE_ERRORS + (asyncio.TimeoutError,)


async def database_unavailable(
    _request: Request | None,
    exc: Exception,
) -> JSONResponse:
    _log.warning("persistência temporariamente indisponível: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Persistence temporarily unavailable. Try again shortly."
        },
        headers={"Retry-After": "30"},
    )


app.add_exception_handler(OperationalError, database_unavailable)
app.add_exception_handler(PoolTimeout, database_unavailable)


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


@app.head("/healthz")
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
        effective_config_dir = _effective_config_dir(None)
        pipeline = load_pipeline(effective_config_dir)
        providers = load_providers(effective_config_dir)
        adapters = providers.get("adapters") if isinstance(providers, dict) else {}
        creator_adapter = adapters.get("creator") if isinstance(adapters, dict) else None
        if (
            creator_adapter == "creator_vercel_elevenlabs_design"
            and not os.environ.get("ELEVENLABS_API_KEY")
        ):
            raise RuntimeError("ELEVENLABS_API_KEY is required")
        video_adapter = adapters.get("video") if isinstance(adapters, dict) else None
        if (
            video_adapter == "replicate"
            and os.environ.get("DATABASE_URL")
            and _truthy_env("ORCH_ENABLE_PAID_ADAPTERS")
        ):
            required = (
                "ORCH_PUBLIC_API_BASE_URL",
                "REPLICATE_WEBHOOK_SIGNING_SECRET",
                "ORCH_WEBHOOK_CORRELATION_SECRET",
            )
            missing = [name for name in required if not os.environ.get(name)]
            if missing:
                raise RuntimeError(
                    "durable Replicate video requires: " + ", ".join(missing)
                )
        backend = resolve_storage_backend(providers)
        if backend == "local":
            pass
        elif backend == "r2":
            R2MediaStorage.from_env()  # valida credenciais R2; não faz request de rede
        else:
            raise ValueError(f"unknown storage backend {backend!r}")
        assembly_adapter = adapters.get("assembly") if isinstance(adapters, dict) else None
        if assembly_adapter == "ffmpeg_assembly":
            missing = [
                binary
                for binary in ("ffmpeg", "ffprobe")
                if shutil.which(binary) is None
            ]
            if missing:
                raise RuntimeError(
                    "required media binaries are missing: " + ", ".join(missing)
                )
            if not (pipeline.get("assembly") or {}).get(
                "final_duration_seconds"
            ):
                raise RuntimeError(
                    "assembly.final_duration_seconds is required for FFmpeg"
                )
        if os.environ.get("DATABASE_URL"):
            async def probe_database() -> None:
                database = await get_shared_database()
                async with database.connection() as connection:
                    await connection.execute("SELECT 1")

            await asyncio.wait_for(probe_database(), timeout=3)
    except _DATABASE_READY_ERRORS:
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "reason": "database unavailable"},
        )
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
    "concepts", "scripts", "creator_profiles", "roster", "review",
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
    "concepts": "Conceitos",
    "scripts": "Scripts",
    "creator_profiles": "Perfis de creators",
    "roster": "Previews de creators",
    "review": "Revisão do plano criativo",
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
    sequence = int(state.get("event_sequence") or len(state.get("buffer") or [])) + 1
    state["event_sequence"] = sequence
    event = {
        **event,
        "event_id": event.get("event_id") or f"local-{sequence}",
        "occurred_at": event.get("occurred_at") or datetime.now(UTC).isoformat(),
    }
    state["buffer"].append(event)
    for q in list(state["queues"]):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _emit(run_id: str, event: dict[str, Any]) -> None:
    _emit_sync(run_id, event)


def _persisted_event_payload(event: Any) -> dict[str, Any]:
    data = event.data if isinstance(event.data, dict) else {}
    if event.event_type in {"awaiting_approval", "awaiting_review"}:
        creators = data.get("creators")
        if isinstance(creators, list):
            data = {
                **data,
                "creators": [
                    _normalize_creator(creator)
                    for creator in creators
                    if isinstance(creator, dict)
                ],
            }
    return {
        **data,
        "type": event.event_type,
        "event_id": str(event.seq),
        "occurred_at": event.created_at.isoformat(),
    }


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
            load_providers(_effective_config_dir(config_dir)),
            root=_media_root,
            web_prefix="/media",
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
    return normalize_creator_payload(creator)


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
    """Lista clips, locução e montagem final com kind e uri."""
    arts: list[dict[str, Any]] = []
    for clip in item.get("clips", []) or []:
        norm = _artifact_dict(clip)
        if norm:
            arts.append(norm)
    voiceover = _artifact_dict(item.get("voiceover"))
    if voiceover:
        arts.append(voiceover)
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
        "attempts", "cost_usd", "dropped", "error", "failure",
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
        "failure": snapshot.get("failure"),
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
        review_future = state.get("review")
        if review_future is not None and not getattr(
            review_future, "done", lambda: False
        )():
            return "review"
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
    campaign_payload: Optional[dict[str, Any]] = None,
    review_plan: Optional[bool] = None,
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
                campaign_payload,
                review_plan,
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
    campaign = CampaignInput.model_validate(
        campaign_payload
        or {
            "offer": offer,
            "audience": "General adult audience",
            "creator_direction": creator_prompt,
            "video_direction": video_prompt,
            "platform": platform,
            "batch_size": batch,
        }
    )
    run_state["campaign"] = campaign.model_dump(mode="json")
    run_state.setdefault("item_snapshots", {})

    try:
        pipeline = load_pipeline(config_dir)
        providers = load_providers(config_dir)
        agent_catalog = load_agent_catalog(config_dir)
        dependencies = RunDependencies.build(
            pipeline, providers, agent_catalog=agent_catalog
        )
        run_state["adapter"] = dependencies.adapter

        cfg: dict[str, Any] = {
            "configurable": dependencies.configurable(
                run_id=run_id,
                platform=platform,
                run_options={
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
                    "review_plan": (
                        review_plan
                        if review_plan is not None
                        else bool(approve_creators or edit_concepts)
                    ),
                },
            ),
            "max_concurrency": int(pipeline.get("batch", {}).get("max_concurrency", 8)),
            "recursion_limit": 100,
        }
        cfg.update(run_trace_config(run_id, offer=offer, platform=platform, batch=batch))
        init: Any = {
            "run_id": run_id,
            "config": {"offer": offer, "batch_size": batch},
            "campaign": campaign.model_dump(mode="json"),
        }

        await _emit(run_id, {"type": "run_start", "run_id": run_id, "offer": offer, "batch": batch})

        final_output: dict[str, Any] = {}
        progress_translator = ProgressEventTranslator()

        async with open_checkpointer(db_path) as cp:
            graph = build_graph(pipeline, checkpointer=cp)
            resume_input = init

            while True:
                async for event in graph.astream_events(resume_input, cfg, version="v2"):
                    etype: str = event["event"]
                    meta = event.get("metadata", {})
                    node = meta.get("langgraph_node") or event.get("name", "")
                    progress_event = progress_translator.translate(event)
                    if progress_event is not None:
                        await _emit(run_id, progress_event)

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
                    if intr_payload.get("type") == "review_creative_plan":
                        concepts = [
                            _safe_serialize(c)
                            for c in intr_payload.get("concepts", [])
                        ]
                        creators = [
                            _normalize_creator(c)
                            for c in intr_payload.get("creators", [])
                        ]
                        review_payload = {
                            "concepts": concepts,
                            "creators": creators,
                        }
                        await _emit(
                            run_id,
                            {
                                "type": "awaiting_review",
                                "run_id": run_id,
                                **review_payload,
                            },
                        )
                        review_future = asyncio.get_event_loop().create_future()
                        run_state_ref = _runs.get(run_id)
                        if run_state_ref is not None:
                            run_state_ref["review"] = review_future
                            run_state_ref["pending_review"] = review_payload
                        persisted_state = _to_plain(dict(snap.values or {}))
                        persisted_state["pending_review"] = review_payload
                        if _run_repository is not None:
                            await _run_repository.save(
                                run_id,
                                phase="review",
                                state=persisted_state,
                                summary=runner.summarize({
                                    **dict(snap.values or {}),
                                    "run_id": run_id,
                                }),
                                items=[],
                            )
                        review_decision = await review_future
                        if review_decision.get("action") == "approve":
                            approved_creators = (
                                review_decision.get("creators") or creators
                            )
                            async with creator_store.open_repository(store_path) as creator_repo:
                                await creator_repo.record_creators(
                                    run_id,
                                    approved_creators,
                                    approved_ids=[
                                        str(c.get("id"))
                                        for c in approved_creators
                                        if c.get("id")
                                    ],
                                    creator_prompt=creator_prompt,
                                    video_prompt=video_prompt,
                                    offer=offer,
                                )
                        resume_input = Command(resume=review_decision)
                        continue
                    raise RuntimeError(
                        f"unsupported human gate: {intr_payload.get('type')!r}"
                    )
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


class RunV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign: CampaignInput
    config_dir: Optional[str] = None
    db: Optional[str] = None


class ReviewConceptPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    offer: Optional[str] = Field(default=None, max_length=8000)
    hook: Optional[str] = Field(default=None, max_length=500)
    angle: Optional[str] = Field(default=None, max_length=1000)
    audience_problem: Optional[str] = Field(default=None, max_length=1000)
    product_mechanism: Optional[str] = Field(default=None, max_length=1000)
    evidence_basis: Optional[
        Literal["provided_fact", "performance", "cold_test"]
    ] = None
    format: Optional[str] = Field(default=None, max_length=200)
    hook_style: Optional[str] = Field(default=None, max_length=200)
    script: Optional[str] = Field(default=None, max_length=12000)
    script_draft: Optional[dict[str, Any]] = None


class ReviewCreatorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    archetype: Optional[str] = Field(default=None, max_length=500)
    visual_brief: Optional[str] = Field(default=None, max_length=2000)
    voice_brief: Optional[str] = Field(default=None, max_length=2000)
    performance_style: Optional[str] = Field(default=None, max_length=1000)
    exclusions: Optional[list[str]] = Field(default=None, max_length=20)
    selected_voice_candidate_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
    )


class ReviewV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    concepts: Optional[list[ReviewConceptPatch]] = None
    creators: Optional[list[ReviewCreatorPatch]] = None
    target: Optional[str] = None
    ids: list[str] = Field(default_factory=list, max_length=48)
    feedback: Optional[str] = Field(default=None, max_length=4000)
    gate_id: Optional[str] = None
    version: Optional[int] = Field(default=None, ge=1)
    gate_type: Optional[Literal["review_creative_plan"]] = None

    @model_validator(mode="after")
    def validate_action(self) -> "ReviewV2Request":
        if self.action == "approve":
            if self.target is not None or self.ids or self.feedback is not None:
                raise ValueError("approve does not accept regeneration fields")
            return self
        if self.action == "regenerate":
            if self.target not in {"concepts", "scripts", "creators", "voices"}:
                raise ValueError(
                    "regenerate requires target concepts, scripts, creators, or voices"
                )
            if not self.ids or len(self.ids) != len(set(self.ids)):
                raise ValueError("regenerate requires unique IDs")
            if self.target in {"concepts", "scripts"} and self.creators is not None:
                raise ValueError("concept regeneration does not accept creator edits")
            if self.target in {"creators", "voices"} and self.concepts is not None:
                raise ValueError("creator regeneration does not accept concept edits")
            return self
        raise ValueError("action must be approve or regenerate")

    def resolution(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action}
        if self.concepts is not None:
            payload["concepts"] = [
                concept.model_dump(exclude_none=True)
                for concept in self.concepts
            ]
        if self.creators is not None:
            payload["creators"] = [
                creator.model_dump(exclude_none=True)
                for creator in self.creators
            ]
        if self.action == "regenerate":
            payload.update(
                target=self.target,
                ids=self.ids,
                feedback=self.feedback or "",
            )
        return payload


def _validated_review_resolution(
    req: ReviewV2Request,
    pending_review: dict[str, Any],
) -> dict[str, Any]:
    resolution = req.resolution()
    concepts = pending_review.get("concepts")
    creators = pending_review.get("creators")
    if not isinstance(concepts, list) or not isinstance(creators, list):
        raise HTTPException(409, "payload canônico da revisão indisponível")

    try:
        if req.action == "approve":
            if req.concepts is not None:
                apply_review_concept_updates(
                    concepts,
                    resolution["concepts"],
                )
            reviewed_creators = creators
            if req.creators is not None:
                reviewed_creators = apply_review_creator_updates(
                    creators,
                    resolution["creators"],
                )
            validate_voice_selections(reviewed_creators)
        elif req.ids:
            available_ids = {
                str(item.get("id"))
                for item in (
                    creators
                    if req.target in {"creators", "voices"}
                    else concepts
                )
                if isinstance(item, dict) and item.get("id")
            }
            if not set(req.ids).issubset(available_ids):
                raise ValueError("regeneration IDs must belong to the pending review")
            if req.creators is not None:
                apply_review_creator_updates(creators, resolution["creators"])
            if req.concepts is not None:
                apply_review_concept_updates(concepts, resolution["concepts"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return resolution


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve o SPA React (front/dist) ou um fallback quando ainda não foi buildado."""
    idx = _front_index()
    if idx is not None:
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse(_UNBUILT_FALLBACK)


@app.post("/api/v2/runs")
async def start_run_v2(
    req: RunV2Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    campaign = req.campaign
    run_id = f"web-{uuid.uuid4().hex[:8]}"
    db_path = req.db or str(default_db_path())
    effective_config_dir = _effective_config_dir(req.config_dir)
    payload = {
        "offer": campaign.offer,
        "batch": campaign.batch_size,
        "platform": campaign.platform,
        "config_dir": effective_config_dir,
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
            _wake_web_embedded_runner()
            return {"run_id": run_id, "job_id": str(queued.job_id)}

    _runs[run_id] = {"queues": [], "buffer": [], "done": False}
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
        effective_config_dir,
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
    effective_config_dir = _effective_config_dir(req.config_dir)
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
                    "config_dir": effective_config_dir,
                    "db_path": db_path,
                    "creator_prompt": req.creator_prompt,
                    "video_prompt": req.video_prompt,
                    "approve_creators": req.approve_creators,
                    "edit_concepts": req.edit_concepts,
                    "seed_creator": seed_creator,
                },
            )
            _wake_web_embedded_runner()
            return {"run_id": run_id, "job_id": str(queued.job_id)}

    # No caminho local, API e executor vivem no mesmo processo. No caminho durável,
    # o job acima guarda só o ponteiro canônico e o Runner assina no consumo.
    if seed_creator is not None:
        seed_creator = await _sign_payload(seed_creator, effective_config_dir)
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
        run_id,
        req.offer,
        req.batch,
        req.platform,
        effective_config_dir,
        db_path,
        req.creator_prompt,
        req.video_prompt,
        req.approve_creators,
        req.edit_concepts,
        seed_creator,
    )
    return {"run_id": run_id}


@app.post("/api/v2/runs/{run_id}/review")
async def review_run_v2(
    run_id: str,
    req: ReviewV2Request,
) -> dict[str, Any]:
    if os.environ.get("DATABASE_URL"):
        if req.gate_id is None or req.version is None:
            raise HTTPException(409, "gate_id e version são obrigatórios")
        if req.gate_type != "review_creative_plan":
            raise HTTPException(
                409,
                "gate_type=review_creative_plan é obrigatório",
            )
        from orchestrator.db import CancelledGateError, StaleGateError

        async with job_store.open_repository() as jobs:
            assert jobs is not None
            try:
                requested_gate_id = uuid.UUID(req.gate_id)
                pending_gate = await jobs.get_pending_gate(run_id)
                if (
                    pending_gate is None
                    or pending_gate.gate_id != requested_gate_id
                    or pending_gate.version != req.version
                    or pending_gate.gate_type != req.gate_type
                ):
                    raise HTTPException(
                        409,
                        "gate não corresponde à revisão pendente deste run",
                    )
                resolution = _validated_review_resolution(
                    req,
                    pending_gate.payload,
                )
                resume = await jobs.resolve_gate(
                    requested_gate_id,
                    version=req.version,
                    resolution=resolution,
                )
            except HTTPException:
                raise
            except CancelledGateError as exc:
                raise HTTPException(410, str(exc)) from exc
            except (ValueError, StaleGateError) as exc:
                raise HTTPException(409, str(exc)) from exc
        _wake_web_embedded_runner()
        return {"ok": True, "job_id": str(resume.job_id)}

    state = _runs.get(run_id)
    future = (state or {}).get("review")
    if future is None or future.done():
        raise HTTPException(409, "nenhuma revisão pendente")
    pending_review = (state or {}).get("pending_review")
    if not isinstance(pending_review, dict):
        raise HTTPException(409, "payload canônico da revisão indisponível")
    resolution = _validated_review_resolution(req, pending_review)
    future.set_result(resolution)
    return {"ok": True}


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


@app.post("/api/run/{run_id}/retry")
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
        _wake_web_embedded_runner()
    return {
        "run_id": new_run_id,
        "source_run_id": run_id,
        "job_id": str(queued.job_id),
    }


class ApproveRequest(BaseModel):
    approved: list[str] = []
    gate_id: Optional[str] = None
    version: Optional[int] = None


@app.post("/api/approve/{run_id}/creators/{creator_id}/reroll-voice")
async def reroll_creator_voice(run_id: str, creator_id: str) -> dict[str, Any]:
    """Compatibility endpoint that resumes the combined gate's voice-only branch."""
    if os.environ.get("DATABASE_URL"):
        raise HTTPException(
            status_code=409,
            detail="use the versioned V2 review endpoint for durable voice rerolls",
        )
    state = _runs.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    pending_review = state.get("pending_review")
    review_future = state.get("review")
    if (
        not isinstance(pending_review, dict)
        or review_future is None
        or review_future.done()
    ):
        raise HTTPException(status_code=409, detail="nenhuma revisão pendente")
    creator_ids = {
        str(creator.get("id") or "")
        for creator in pending_review.get("creators") or []
        if isinstance(creator, dict)
    }
    if creator_id not in creator_ids:
        raise HTTPException(status_code=404, detail=f"creator {creator_id!r} not found")

    review_future.set_result(
        {
            "action": "regenerate",
            "target": "voices",
            "ids": [creator_id],
            "feedback": "",
        }
    )
    return {"ok": True, "queued": True, "creator_id": creator_id}


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
        _wake_web_embedded_runner()
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
        _wake_web_embedded_runner()
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
    effective_config_dir = _effective_config_dir(config_dir)
    providers = load_providers(effective_config_dir)
    agent_catalog = load_agent_catalog(effective_config_dir)
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
                            f"id: {event.seq}\n"
                            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
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
            except _DATABASE_UNAVAILABLE_ERRORS as exc:
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

    state = _runs.get(run_id)
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
                event_id = event.get("event_id")
                prefix = f"id: {event_id}\n" if event_id else ""
                yield f"{prefix}data: {json.dumps(event, ensure_ascii=False)}\n\n"
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
                if entry.phase != "cancelled"
                and (entry.phase == "error" or entry.error)
            ]
            persisted_cancelled = [
                entry.run_id
                for entry in index
                if entry.phase == "cancelled"
            ]
            persisted_active = [
                entry.run_id
                for entry in index
                if entry.phase in {"running", "editing", "awaiting", "review"}
                and not entry.error
            ]
    if postgres_enabled:
        errored = persisted_errors
        cancelled = persisted_cancelled
        active = persisted_active
        known = persisted_ids
    else:
        cancelled = [
            rid
            for rid, state in _runs.items()
            if state.get("phase") == "cancelled"
        ]
        errored = [
            rid
            for rid, state in _runs.items()
            if state.get("error") and state.get("phase") != "cancelled"
        ]
        active = [
            rid
            for rid, state in _runs.items()
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


@app.get("/api/status/{run_id}")
async def run_status(run_id: str, config_dir: Optional[str] = None, db: Optional[str] = None) -> Any:
    pipeline = load_pipeline(_effective_config_dir(config_dir))
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
    effective_config_dir = _effective_config_dir(config_dir)
    pipeline = load_pipeline(effective_config_dir)
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
    pending_gate = None
    persisted_events: list[dict[str, Any]] = []
    if runtime_state is None and persisted is not None:
        async with job_store.open_repository() as jobs:
            if jobs is not None:
                list_events = getattr(jobs, "list_events", None)
                if list_events is not None:
                    persisted_events = [
                        _persisted_event_payload(event)
                        for event in await list_events(run_id)
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
            if pending_gate is not None
            and pending_gate.gate_type == "review_creative_plan"
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
        edit_concepts = [
            _safe_serialize(c)
            for c in concepts_source or []
            if isinstance(c, dict)
        ]
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
        awaiting = [
            _normalize_creator(c)
            for c in creators_source or []
            if isinstance(c, dict)
        ]

    progress_events = (
        list(runtime_state.get("buffer") or [])
        if runtime_state is not None
        else persisted_events
    )
    progress = build_progress(
        progress_events,
        phase=phase,
        items=items,
        batch_size=persisted.batch_size if persisted is not None else None,
    )
    activity = build_activity(progress_events)

    return await _sign_payload(
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
                else persisted.error if persisted is not None else None
            ),
        },
        effective_config_dir,
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
