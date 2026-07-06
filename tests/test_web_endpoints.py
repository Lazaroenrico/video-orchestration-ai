"""Cobertura dos endpoints e helpers do dashboard web (chamados como coroutines).

Segue o padrão do repo: nada de TestClient — as rotas são coroutines chamadas
diretamente, com asserção em ``HTTPException`` para os caminhos de erro. O estado
global ``web_server._runs`` é limpo por fixture.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from orchestrator import runner
from orchestrator.graph.state import Artifact
from orchestrator.web import server as web_server

_MOCK_PROVIDERS = {
    "adapters": {r: "mock" for r in ("llm", "creator", "video", "qc", "assembly")}
}


@pytest.fixture(autouse=True)
def _clean_runs():
    web_server._runs.clear()
    yield
    web_server._runs.clear()


def _drain(q: asyncio.Queue) -> list:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ------------------------------------------------------------------ #
# _emit_sync                                                         #
# ------------------------------------------------------------------ #

def test_emit_sync_noop_for_unknown_run():
    web_server._emit_sync("nope", {"type": "x"})  # não deve levantar


def test_emit_sync_buffers_when_queue_full():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait({"type": "old"})
    web_server._runs["r"] = {"buffer": [], "queues": [q]}

    web_server._emit_sync("r", {"type": "new"})

    assert web_server._runs["r"]["buffer"] == [{"type": "new"}]  # buffer sempre
    assert q.qsize() == 1  # fila cheia: evento descartado sem erro


# ------------------------------------------------------------------ #
# helpers de normalização                                           #
# ------------------------------------------------------------------ #

def test_pending_creators_for_unknown_run_raises_404():
    with pytest.raises(HTTPException) as ei:
        web_server._pending_creators_for("nope")
    assert ei.value.status_code == 404


def test_pending_creators_for_empty_raises_409():
    web_server._runs["r"] = {"pending_creators": []}
    with pytest.raises(HTTPException) as ei:
        web_server._pending_creators_for("r")
    assert ei.value.status_code == 409


def test_recover_creators_from_media_missing_root(tmp_path):
    assert web_server._recover_creators_from_media(tmp_path / "nope") == []


def test_artifact_dict_accepts_pydantic_model():
    art = Artifact(kind="clip", uri="/media/run/x.mp4")
    assert web_server._artifact_dict(art)["uri"] == "/media/run/x.mp4"


def test_normalize_qc_none_for_non_dict():
    assert web_server._normalize_qc(None) is None
    assert web_server._normalize_qc("nope") is None


def test_item_id_from_falls_back_to_last_result():
    data = {"input": {}, "output": {"results": [{"id": "item-1"}, {"id": "item-9"}]}}
    assert web_server._item_id_from(data, {}) == "item-9"


def test_item_id_from_returns_none_when_no_id():
    assert web_server._item_id_from({"output": {"results": []}}, {}) is None


def test_build_item_update_none_for_untracked_node():
    assert web_server._build_item_update("r", "roster", {}, {}) is None


def test_build_item_update_none_when_no_item_id():
    data = {"input": {}, "output": {}}
    assert web_server._build_item_update("r", "script", data, {}) is None


def test_build_item_update_process_item_without_id_returns_none():
    data = {"output": {"results": [{"script": "sem id"}]}}
    assert web_server._build_item_update("r", "process_item", data, {}) is None


def test_safe_serialize_stringifies_beyond_max_depth():
    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    out = web_server._safe_serialize(deep)
    assert isinstance(out["a"]["b"]["c"]["d"], str)


def test_safe_serialize_stringifies_non_json_object():
    class Weird:
        def __repr__(self) -> str:
            return "weird"

    out = web_server._safe_serialize({"x": Weird()})
    assert out["x"] == "weird"


# ------------------------------------------------------------------ #
# dashboard                                                         #
# ------------------------------------------------------------------ #

async def test_dashboard_returns_html():
    resp = await web_server.dashboard()
    assert resp.status_code == 200
    assert b"<" in resp.body


# ------------------------------------------------------------------ #
# reroll-voice endpoint                                             #
# ------------------------------------------------------------------ #

async def test_reroll_endpoint_409_without_adapter():
    web_server._runs["r"] = {"pending_creators": [{"id": "creator-0"}]}
    with pytest.raises(HTTPException) as ei:
        await web_server.reroll_creator_voice("r", "creator-0")
    assert ei.value.status_code == 409


async def test_reroll_endpoint_404_when_creator_absent():
    class _Ad:
        pass

    web_server._runs["r"] = {"pending_creators": [{"id": "creator-0"}], "adapter": _Ad()}
    with pytest.raises(HTTPException) as ei:
        await web_server.reroll_creator_voice("r", "creator-X")
    assert ei.value.status_code == 404


async def test_reroll_endpoint_success_emits_update():
    class _Ad:  # fallback: sem método reroll → caminho determinístico offline
        pass

    creator = {
        "id": "creator-0",
        "upscaled_base": "data:image/png;base64,IMG",
        "voice_id": "voice-0",
        "voice_profile": {"preset": "male", "prompt": "warm"},
    }
    web_server._runs["r"] = {
        "pending_creators": [creator], "adapter": _Ad(), "buffer": [], "queues": [],
    }

    result = await web_server.reroll_creator_voice("r", "creator-0")

    assert result["ok"] is True
    assert result["creator"]["id"] == "creator-0"
    assert any(e.get("type") == "creator_update" for e in web_server._runs["r"]["buffer"])


# ------------------------------------------------------------------ #
# approve endpoint                                                  #
# ------------------------------------------------------------------ #

async def test_approve_409_without_pending_future():
    web_server._runs["r"] = {}
    with pytest.raises(HTTPException) as ei:
        await web_server.approve("r", web_server.ApproveRequest(approved=[]))
    assert ei.value.status_code == 409


# ------------------------------------------------------------------ #
# submit_concepts endpoint (gate de edição)                          #
# ------------------------------------------------------------------ #

async def test_submit_concepts_409_without_pending_future():
    web_server._runs["rc"] = {}
    with pytest.raises(HTTPException) as ei:
        await web_server.submit_concepts("rc", web_server.ConceptEditRequest(concepts=[]))
    assert ei.value.status_code == 409


async def test_submit_concepts_resolves_pending_future():
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    web_server._runs["rc"] = {"concept_edit": fut}
    edited = [{"id": "c-0", "script": "EDITED"}]

    out = await web_server.submit_concepts("rc", web_server.ConceptEditRequest(concepts=edited))

    assert out == {"ok": True, "count": 1}
    assert fut.result() == {"concepts": edited}


# ------------------------------------------------------------------ #
# stream_events (SSE)                                               #
# ------------------------------------------------------------------ #

async def test_stream_events_404_unknown_run():
    with pytest.raises(HTTPException) as ei:
        await web_server.stream_events("nope")
    assert ei.value.status_code == 404


async def test_stream_events_replays_buffer_and_ends_when_done():
    web_server._runs["r"] = {"buffer": [{"type": "hello"}], "queues": [], "done": True}

    resp = await web_server.stream_events("r")
    body = "".join([c async for c in resp.body_iterator])

    assert "hello" in body
    assert "stream_end" in body


async def test_stream_events_emits_keepalive_on_timeout(monkeypatch):
    web_server._runs["r"] = {"buffer": [], "queues": [], "done": False}
    calls = {"n": 0}

    async def fake_wait_for(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError
        return None  # sentinel → stream_end

    monkeypatch.setattr(web_server.asyncio, "wait_for", fake_wait_for)

    resp = await web_server.stream_events("r")
    body = "".join([c async for c in resp.body_iterator])

    assert "keepalive" in body
    assert "stream_end" in body


# ------------------------------------------------------------------ #
# /api/runs e /api/status                                          #
# ------------------------------------------------------------------ #

async def test_list_runs_endpoint_empty_for_missing_db(tmp_path):
    out = await web_server.list_runs_endpoint(db=str(tmp_path / "missing.db"))
    assert out["runs"] == []
    assert isinstance(out["active"], list)


def test_runner_list_runs_handles_db_without_checkpoints_table(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()  # existe, mas sem a tabela checkpoints
    assert runner.list_runs(db) == []


async def test_run_status_404_for_unknown_run(tmp_path):
    with pytest.raises(HTTPException) as ei:
        await web_server.run_status(
            "nope", config_dir="config", db=str(tmp_path / "cp.db")
        )
    assert ei.value.status_code == 404


# ------------------------------------------------------------------ #
# _execute_run — fluxo completo (mock) e caminho de erro             #
# ------------------------------------------------------------------ #

async def test_execute_run_completes_with_mock_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(web_server, "load_providers", lambda *a, **k: _MOCK_PROVIDERS)
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))

    q: asyncio.Queue = asyncio.Queue()
    web_server._runs["run-x"] = {"queues": [q], "buffer": [], "done": False}

    await web_server._execute_run(
        "run-x", offer="serum X", batch=2, platform="tiktok",
        config_dir="config", db_path=str(tmp_path / "cp.db"),
        approve_creators=False, edit_concepts=False,
    )

    state = web_server._runs["run-x"]
    assert state["done"] is True
    types_ = [e.get("type") for e in state["buffer"]]
    assert "run_start" in types_
    assert "run_end" in types_
    # finally enfileirou o sentinel de fechamento (None) por último nas filas ativas
    drained = _drain(q)
    assert drained[-1] is None

    # o run existe no checkpoint → /api/status devolve o resumo (não 404)
    status = await web_server.run_status(
        "run-x", config_dir="config", db=str(tmp_path / "cp.db")
    )
    assert isinstance(status, dict)
    assert status["run_id"] == "run-x"


async def test_execute_run_emits_error_on_failure(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("config quebrado")

    monkeypatch.setattr(web_server, "load_pipeline", boom)

    q: asyncio.Queue = asyncio.Queue()
    web_server._runs["run-err"] = {"queues": [q], "buffer": [], "done": False}

    await web_server._execute_run(
        "run-err", offer="o", batch=1, platform="tiktok",
        config_dir=None, db_path=str(tmp_path / "cp.db"),
        approve_creators=False, edit_concepts=False,
    )

    state = web_server._runs["run-err"]
    assert state["done"] is True
    assert any(e.get("type") == "error" for e in state["buffer"])
    assert _drain(q)[-1] is None  # sentinel do finally por último