"""Serviço do SPA React (front/dist) pelo FastAPI.

Padrão do repo: sem TestClient — rotas chamadas como coroutines, erros via
``HTTPException``. Cobre: fallback quando o front não foi buildado, serviço do
index buildado, catch-all SPA que não sombreia /api|/media|/videos|/assets, e o
endpoint /api/integrations que lê providers.yaml.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.web import server as web_server


@pytest.mark.asyncio
async def test_dashboard_fallback_when_front_not_built(monkeypatch) -> None:
    monkeypatch.setattr(web_server, "_front_index", lambda: None)
    resp = await web_server.dashboard()
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "<" in body
    assert "npm run build" in body  # instrui a buildar o front


@pytest.mark.asyncio
async def test_dashboard_serves_built_index(monkeypatch, tmp_path) -> None:
    idx = tmp_path / "index.html"
    idx.write_text("<!doctype html><title>SPA-SENTINEL</title>", encoding="utf-8")
    monkeypatch.setattr(web_server, "_front_index", lambda: idx)
    resp = await web_server.dashboard()
    assert resp.status_code == 200
    assert "SPA-SENTINEL" in resp.body.decode()


@pytest.mark.asyncio
async def test_spa_fallback_serves_index_for_client_route(monkeypatch, tmp_path) -> None:
    idx = tmp_path / "index.html"
    idx.write_text("<!doctype html><title>SPA-SENTINEL</title>", encoding="utf-8")
    monkeypatch.setattr(web_server, "_front_index", lambda: idx)
    resp = await web_server.spa_fallback("campaigns/web-123")
    assert resp.status_code == 200
    assert "SPA-SENTINEL" in resp.body.decode()


@pytest.mark.asyncio
async def test_spa_fallback_does_not_shadow_api_media_assets() -> None:
    for path in ("api/unknown", "media/x.png", "videos/y.mp4", "assets/app.js"):
        with pytest.raises(HTTPException) as ei:
            await web_server.spa_fallback(path)
        assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_spa_fallback_uses_unbuilt_fallback_when_no_index(monkeypatch) -> None:
    monkeypatch.setattr(web_server, "_front_index", lambda: None)
    resp = await web_server.spa_fallback("analytics")
    assert resp.status_code == 200
    assert "npm run build" in resp.body.decode()


def test_front_index_returns_path_when_built(monkeypatch, tmp_path) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(web_server, "_FRONT_DIST", tmp_path)
    assert web_server._front_index() == tmp_path / "index.html"


def test_front_index_none_when_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(web_server, "_FRONT_DIST", tmp_path / "nope")
    assert web_server._front_index() is None


def test_cors_origins_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "ORCH_CORS_ORIGINS",
        "https://front.example.com, http://localhost:5173 ,,",
    )
    assert web_server._cors_origins_from_env() == [
        "https://front.example.com",
        "http://localhost:5173",
    ]


def test_install_cors_is_opt_in() -> None:
    app = FastAPI()
    web_server._install_cors(app, [])
    assert app.user_middleware == []

    web_server._install_cors(app, ["https://front.example.com"])
    assert app.user_middleware[0].cls is CORSMiddleware
    assert app.user_middleware[0].kwargs["allow_origins"] == ["https://front.example.com"]


@pytest.mark.asyncio
async def test_integrations_reads_provider_adapters(monkeypatch) -> None:
    from orchestrator.agent_catalog import default_agent_catalog

    monkeypatch.setattr(
        web_server,
        "load_providers",
        lambda config_dir=None: {"adapters": {"video": "replicate", "llm": "gateway"}},
    )
    monkeypatch.setattr(
        web_server,
        "load_agent_catalog",
        lambda config_dir=None: default_agent_catalog(),
    )
    out = await web_server.integrations_index()
    assert out["stages"] == {"video": "replicate", "llm": "gateway"}
    assert out["agents"]["stages"]["concepts"]["executor"] == "tool"
    assert out["agents"]["stages"]["concepts"]["tools"] == ["generate_concepts"]


@pytest.mark.asyncio
async def test_integrations_empty_when_no_adapters(monkeypatch) -> None:
    monkeypatch.setattr(web_server, "load_providers", lambda config_dir=None: {})
    out = await web_server.integrations_index()
    assert out["stages"] == {}


# --------------------------------------------------------------------------- #
# Health/readiness (ADR-D36 Fase 1): liveness sem IO; readiness valida config   #
# e credenciais de storage sem chamar provider pago.                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_healthz_is_ok_without_touching_config() -> None:
    assert await web_server.healthz() == {"status": "ok"}


def _stub_config(monkeypatch, *, storage_backend=None) -> None:
    monkeypatch.setattr(web_server, "load_pipeline", lambda path=None: {})
    monkeypatch.setattr(web_server, "load_judge", lambda path=None: {})
    providers = {"storage": {"backend": storage_backend}} if storage_backend else {}
    monkeypatch.setattr(web_server, "load_providers", lambda path=None: providers)


@pytest.mark.asyncio
async def test_readyz_ready_for_local_backend(monkeypatch) -> None:
    _stub_config(monkeypatch, storage_backend="local")
    resp = await web_server.readyz()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body == {"status": "ready", "storage": "local"}


@pytest.mark.asyncio
async def test_readyz_ready_defaults_to_local_when_unset(monkeypatch) -> None:
    _stub_config(monkeypatch)
    resp = await web_server.readyz()
    assert resp.status_code == 200
    assert json.loads(resp.body)["storage"] == "local"


@pytest.mark.asyncio
async def test_readyz_not_ready_when_r2_credentials_missing(monkeypatch) -> None:
    _stub_config(monkeypatch, storage_backend="r2")
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    resp = await web_server.readyz()
    assert resp.status_code == 503
    assert json.loads(resp.body)["status"] == "not-ready"


@pytest.mark.asyncio
async def test_readyz_not_ready_for_unknown_backend(monkeypatch) -> None:
    _stub_config(monkeypatch, storage_backend="weird")
    resp = await web_server.readyz()
    assert resp.status_code == 503
    assert "weird" in json.loads(resp.body)["reason"]


@pytest.mark.asyncio
async def test_readyz_not_ready_when_config_fails_to_load(monkeypatch) -> None:
    def boom(path=None):
        raise RuntimeError("config quebrada")

    monkeypatch.setattr(web_server, "load_pipeline", boom)
    resp = await web_server.readyz()
    assert resp.status_code == 503
    assert "config quebrada" in json.loads(resp.body)["reason"]


# --------------------------------------------------------------------------- #
# Mounts /media e /videos condicionais (ADR-D36 Fase 1): default ligado, mas    #
# desligáveis em produção (storage R2 serve por URL assinada).                  #
# --------------------------------------------------------------------------- #


def test_media_mounts_installed_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ORCH_SERVE_LOCAL_MEDIA", raising=False)
    app = FastAPI()
    web_server._install_media_mounts(app)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/media" in paths and "/videos" in paths


def test_media_mounts_skipped_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SERVE_LOCAL_MEDIA", "0")
    app = FastAPI()
    web_server._install_media_mounts(app)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/media" not in paths and "/videos" not in paths
