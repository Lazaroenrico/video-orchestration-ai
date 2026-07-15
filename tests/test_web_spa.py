"""Serviço do SPA React (front/dist) pelo FastAPI.

Padrão do repo: sem TestClient — rotas chamadas como coroutines, erros via
``HTTPException``. Cobre: fallback quando o front não foi buildado, serviço do
index buildado, catch-all SPA que não sombreia /api|/media|/videos|/assets, e o
endpoint /api/integrations que lê providers.yaml.
"""
from __future__ import annotations

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
    monkeypatch.setattr(
        web_server,
        "load_providers",
        lambda config_dir=None: {"adapters": {"video": "replicate", "llm": "gateway"}},
    )
    out = await web_server.integrations_index()
    assert out["stages"] == {"video": "replicate", "llm": "gateway"}


@pytest.mark.asyncio
async def test_integrations_empty_when_no_adapters(monkeypatch) -> None:
    monkeypatch.setattr(web_server, "load_providers", lambda config_dir=None: {})
    out = await web_server.integrations_index()
    assert out["stages"] == {}
