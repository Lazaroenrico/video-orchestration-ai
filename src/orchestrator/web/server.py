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
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

import orchestrator.creator_store as creator_store
import orchestrator.job_store as job_store
import orchestrator.prompt_store as prompt_store
import orchestrator.run_store as run_store
from orchestrator import runner, stream_bus
from orchestrator.auth import CloudflareAccessMiddleware
from orchestrator.config import (
    default_creator_store_path,
    default_media_path,
    default_videos_path,
    load_agent_catalog,
    load_pipeline,
    load_providers,
)
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
from orchestrator.graph.topology import ITEM_UPDATE_NODES, NODE_LABELS, PIPELINE_NODES
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
from orchestrator.web import routes_content, routes_review, routes_runs, runs_registry
from orchestrator.web.events import (
    DATABASE_UNAVAILABLE_ERRORS,
    _artifact_dict,
    _build_item_update,
    _complete_item_payload,
    _creator_id,
    _extract_artifacts,
    _has_complete_media,
    _is_renderable_uri,
    _item_id_from,
    _item_payload_from_result,
    _media_type_for_uri,
    _merge_artifacts,
    _merge_item_snapshot,
    _normalize_artifact,
    _normalize_creator,
    _normalize_creator_history,
    _normalize_qc,
    _persisted_event_payload,
    _playable_voice_uri,
    _runtime_phase,
    _safe_serialize,
    _snapshot_from_item,
)
from orchestrator.web.routes_content import (
    PromptTemplateRequest,
    _find_creator_for_draft_repository,
    _pick_first_existing,
    _recover_creators_from_media,
    creators_history,
    delete_prompt_template,
    integrations_index,
    prompts_index,
    save_prompt_template,
)
from orchestrator.web.routes_review import (
    ApproveRequest,
    ConceptEditRequest,
    ReviewConceptPatch,
    ReviewCreatorPatch,
    ReviewV2Request,
    _validated_review_resolution,
    approve,
    reroll_creator_voice,
    review_run_v2,
    submit_concepts,
)
from orchestrator.web.routes_runs import (
    RunRequest,
    RunV2Request,
    _retry_payload_fields,
    list_runs_endpoint,
    retry_run,
    run_state,
    run_status,
    start_run,
    start_run_v2,
    stream_events,
)
from orchestrator.web.run_executor import (
    _RUN_REPOSITORY_UNSET,
    _emit,
    _emit_sync,
    _execute_run,
    _server_attr,
)
from orchestrator.web.runs_registry import (
    RunRegistry,
)
from orchestrator.web.runs_registry import (
    pending_creators_for as _pending_creators_for,
)
from orchestrator.web.settings import effective_config_dir as _effective_config_dir
from orchestrator.web.settings import truthy_env as _truthy_env
from orchestrator.worker import run_worker_once

# Retrocompatibilidade: símbolos que viviam neste módulo e hoje são extraídos
# para web.events / web.run_executor / web.runs_registry / web.routes_*.
__all__ = [
    "DATABASE_UNAVAILABLE_ERRORS",
    "ApproveRequest",
    "BackgroundTasks",
    "ConceptEditRequest",
    "ITEM_UPDATE_NODES",
    "NODE_LABELS",
    "PIPELINE_NODES",
    "PromptTemplateRequest",
    "ReviewConceptPatch",
    "ReviewCreatorPatch",
    "ReviewV2Request",
    "RunDependencies",
    "RunRegistry",
    "RunRequest",
    "RunV2Request",
    "_RUN_REPOSITORY_UNSET",
    "_artifact_dict",
    "_build_item_update",
    "_complete_item_payload",
    "_creator_id",
    "_effective_config_dir",
    "_emit",
    "_emit_sync",
    "_execute_run",
    "_extract_artifacts",
    "_find_creator_for_draft_repository",
    "_has_complete_media",
    "_is_renderable_uri",
    "_item_id_from",
    "_item_payload_from_result",
    "_merge_artifacts",
    "_merge_item_snapshot",
    "_media_type_for_uri",
    "_normalize_artifact",
    "_normalize_creator",
    "_normalize_creator_history",
    "_normalize_qc",
    "_pending_creators_for",
    "_persisted_event_payload",
    "_pick_first_existing",
    "_playable_voice_uri",
    "_recover_creators_from_media",
    "_retry_payload_fields",
    "_runtime_phase",
    "_safe_serialize",
    "_server_attr",
    "_snapshot_from_item",
    "_truthy_env",
    "_validated_review_resolution",
    "approve",
    "build_graph",
    "creators_history",
    "creator_store",
    "default_creator_store_path",
    "delete_prompt_template",
    "integrations_index",
    "job_store",
    "list_runs_endpoint",
    "load_agent_catalog",
    "open_checkpointer",
    "prompt_store",
    "prompts_index",
    "reroll_creator_voice",
    "retry_run",
    "review_run_v2",
    "run_state",
    "run_status",
    "run_store",
    "runner",
    "save_prompt_template",
    "start_run",
    "start_run_v2",
    "stream_events",
    "stream_bus",
    "submit_concepts",
    "run_trace_config",
]


_log = logging.getLogger(__name__)
_web_runner_wake_event: asyncio.Event | None = None


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


_DATABASE_UNAVAILABLE_ERRORS = DATABASE_UNAVAILABLE_ERRORS
_DATABASE_READY_ERRORS = _DATABASE_UNAVAILABLE_ERRORS + (asyncio.TimeoutError,)


async def database_unavailable(
    _request: Request | None,
    exc: Exception,
) -> JSONResponse:
    _log.warning("persistência temporariamente indisponível: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "Persistence temporarily unavailable. Try again shortly."},
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
        if creator_adapter == "creator_vercel_elevenlabs_design" and not os.environ.get(
            "ELEVENLABS_API_KEY"
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
                raise RuntimeError("durable Replicate video requires: " + ", ".join(missing))
        backend = resolve_storage_backend(providers)
        if backend == "local":
            pass
        elif backend == "r2":
            R2MediaStorage.from_env()  # valida credenciais R2; não faz request de rede
        else:
            raise ValueError(f"unknown storage backend {backend!r}")
        assembly_adapter = adapters.get("assembly") if isinstance(adapters, dict) else None
        if assembly_adapter == "ffmpeg_assembly":
            missing = [binary for binary in ("ffmpeg", "ffprobe") if shutil.which(binary) is None]
            if missing:
                raise RuntimeError("required media binaries are missing: " + ", ".join(missing))
            if not (pipeline.get("assembly") or {}).get("final_duration_seconds"):
                raise RuntimeError("assembly.final_duration_seconds is required for FFmpeg")
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
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<title>Orchestrator AI</title></head><body style="font-family:system-ui;'
    'max-width:640px;margin:80px auto;padding:0 24px;color:#191c1e">'
    "<h1>Front-end not built</h1><p>The React UI lives in <code>front/</code>. "
    "Build it once, then reload:</p>"
    '<pre style="background:#f2f4f6;padding:16px;border-radius:8px">'
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
    return os.environ.get("ORCH_SERVE_LOCAL_MEDIA", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "",
    )


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

# run_id → {queues: list[Queue], buffer: list[dict], done: bool} — registro em
# memória (web.runs_registry); instância única também exposta em app.state.runs.
_runs = runs_registry.REGISTRY
app.state.runs = runs_registry.REGISTRY


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


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve o SPA React (front/dist) ou um fallback quando ainda não foi buildado."""
    idx = _front_index()
    if idx is not None:
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse(_UNBUILT_FALLBACK)


# Rotas vivem em módulos por domínio; o composition root apenas as compõe, na
# mesma ordem relativa de registro anterior (spa_fallback segue por último).
app.include_router(routes_runs.router)
app.include_router(routes_review.router)
app.include_router(routes_content.router)
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
