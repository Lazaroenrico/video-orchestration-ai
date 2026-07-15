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


async def _wait_for_run_key(run_id: str, key: str, task: asyncio.Task, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = web_server._runs[run_id]
        if key in state:
            return state
        assert not task.done(), f"run finished before {key!r} was available"
        await asyncio.sleep(0.02)
    raise AssertionError(f"run did not expose {key!r}")


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


def test_creator_id_returns_none_without_id_alias():
    assert web_server._creator_id({"name": "Creator"}) is None


def test_find_creator_for_draft_recovers_from_media_and_scopes_run(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    creator_dir = media_root / "web-old" / "creator-0"
    creator_dir.mkdir(parents=True)
    (creator_dir / "image.png").write_bytes(b"png")
    (creator_dir / "voice.wav").write_bytes(b"wav")
    monkeypatch.setattr(web_server, "default_creator_store_path", lambda: tmp_path / "missing.json")
    monkeypatch.setattr(web_server, "default_media_path", lambda: media_root)
    monkeypatch.setattr(
        web_server.creator_store,
        "load_creators",
        lambda path: [{"id": "creator-other"}],
    )

    creator = web_server._find_creator_for_draft("creator-0", "web-old")

    assert creator["id"] == "creator-0"
    assert creator["image_uri"] == "/media/web-old/creator-0/image.png"
    assert creator["voice_preview_uri"] == "/media/web-old/creator-0/voice.wav"

    with pytest.raises(HTTPException) as ei:
        web_server._find_creator_for_draft("creator-0", "web-other")
    assert ei.value.status_code == 404
    assert "web-other" in ei.value.detail


def test_runtime_phase_branches():
    class _Pending:
        def done(self) -> bool:
            return False

    class _Done:
        def done(self) -> bool:
            return True

    assert web_server._runtime_phase(None, None) == "idle"
    assert web_server._runtime_phase(None, {"in_flight": 1}) == "running"
    assert web_server._runtime_phase(None, {"in_flight": 0}) == "done"
    assert web_server._runtime_phase({"concept_edit": _Pending()}, None) == "editing"
    assert web_server._runtime_phase(
        {"concept_edit": _Done(), "approval": _Pending()}, None
    ) == "awaiting"
    assert web_server._runtime_phase({"approval": _Done(), "done": True}, None) == "done"
    assert web_server._runtime_phase({"done": False}, None) == "running"
    # Um run que quebrou reporta "error", e o erro vence o "done" setado no finally.
    assert web_server._runtime_phase({"error": "boom"}, None) == "error"
    assert web_server._runtime_phase({"error": "boom", "done": True}, None) == "error"


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
    assert out["errored"] == []


async def test_list_runs_endpoint_reports_errored_and_excludes_from_active(tmp_path):
    web_server._runs["running-run"] = {"queues": [], "buffer": [], "done": False}
    web_server._runs["done-run"] = {"queues": [], "buffer": [], "done": True}
    web_server._runs["errored-run"] = {
        "queues": [], "buffer": [], "done": True, "error": "boom",
    }

    out = await web_server.list_runs_endpoint(db=str(tmp_path / "missing.db"))

    # active = só o que está realmente rodando (nem concluído, nem quebrado).
    assert out["active"] == ["running-run"]
    assert out["errored"] == ["errored-run"]


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


async def test_run_state_404_for_unknown_run(tmp_path):
    with pytest.raises(HTTPException) as ei:
        await web_server.run_state(
            "nope", config_dir="config-mock", db=str(tmp_path / "cp.db")
        )
    assert ei.value.status_code == 404


async def test_run_state_returns_runtime_summary_without_checkpoint(tmp_path):
    run_id = "runtime-done"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [{
            "type": "run_end",
            "summary": {
                "run_id": run_id,
                "produced": 1,
                "approved": 1,
                "dropped": 0,
                "in_flight": 0,
                "total_attempts": 1,
                "total_cost_usd": 0.0,
                "cost_by_tier": {},
                "winning_styles": [],
            },
        }],
        "done": True,
    }

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(tmp_path / "cp.db"))

    assert state["phase"] == "done"
    assert state["summary"]["produced"] == 1
    assert state["items"] == []
    assert state["error"] is None


async def test_run_state_surfaces_run_crash_error(tmp_path):
    """Quando a pipeline quebra, /api/state expõe phase="error" + a mensagem,
    para que a falha persista após reconexão (não some com o fim do SSE)."""
    run_id = "crashed-run"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [{"type": "error", "message": "adapter exploded"}],
        "done": True,
        "error": "adapter exploded",
    }

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(tmp_path / "cp.db"))

    assert state["phase"] == "error"
    assert state["error"] == "adapter exploded"


async def test_run_state_merges_runtime_snapshots_and_skips_invalid(tmp_path, monkeypatch):
    run_id = "runtime-snap"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [],
        "done": False,
        "item_snapshots": {
            "fallback-id": {"script": "SCRIPT", "concept": {"hook": "h"}},
            "bad": "not a snapshot",
        },
    }

    async def fake_get_status(pipeline, *, db_path, run_id):
        return {"results": [{"id": "", "concept": {}, "script": "checkpoint sem id"}]}

    monkeypatch.setattr(web_server.runner, "get_status", fake_get_status)

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(tmp_path / "cp.db"))

    assert state["phase"] == "running"
    assert len(state["items"]) == 1
    assert state["items"][0]["id"] == "fallback-id"
    assert state["items"][0]["script"] == "SCRIPT"


async def test_run_state_surfaces_orphaned_pending_items_with_error(tmp_path, monkeypatch):
    """Item que quebrou na montagem (fora de `results`) mas tem clips reais deve
    aparecer no /api/state com seus artifacts + o motivo do erro."""
    from orchestrator.graph.state import Item

    run_id = "orphan-web"

    async def fake_get_status(pipeline, *, db_path, run_id):
        return {"results": []}  # canal results vazio

    async def fake_get_pending_items(pipeline, *, db_path, run_id):
        return [Item(
            id="concept-0001",
            concept={"hook": "h"},
            clips=[Artifact(kind="clip", uri="/videos/orphan-web/items/concept-0001/clip-0.mp4")],
            error="assembly: Seedance bridge failed: input image may contain real person",
        )]

    monkeypatch.setattr(web_server.runner, "get_status", fake_get_status)
    monkeypatch.setattr(web_server.runner, "get_pending_items", fake_get_pending_items)

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(tmp_path / "cp.db"))

    assert len(state["items"]) == 1
    item = state["items"][0]
    assert item["id"] == "concept-0001"
    assert "real person" in item["error"]
    assert item["assembled"] is None
    assert any(a["media_type"] == "video" for a in item["artifacts"])


async def test_run_state_tolerates_pending_recovery_failure(tmp_path, monkeypatch):
    """Se a recuperação de órfãos falhar, /api/state degrada para os results normais."""
    run_id = "orphan-fail"

    async def fake_get_status(pipeline, *, db_path, run_id):
        return {"results": [{"id": "concept-a", "concept": {}, "script": "ok"}]}

    async def boom_pending(pipeline, *, db_path, run_id):
        raise RuntimeError("checkpoint ilegível")

    monkeypatch.setattr(web_server.runner, "get_status", fake_get_status)
    monkeypatch.setattr(web_server.runner, "get_pending_items", boom_pending)

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(tmp_path / "cp.db"))

    assert [it["id"] for it in state["items"]] == ["concept-a"]


async def test_run_state_returns_pending_creators_during_approval_gate(tmp_path):
    run_id = "runtime-awaiting"
    fut = asyncio.get_running_loop().create_future()
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [],
        "done": False,
        "approval": fut,
        "pending_creators": [{
            "creator_id": "creator-0",
            "image": "/media/runtime-awaiting/creator-0/image.png",
            "voice": "/media/runtime-awaiting/creator-0/voice.wav",
        }],
    }

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(tmp_path / "cp.db"))

    assert state["phase"] == "awaiting"
    assert state["awaiting"][0]["id"] == "creator-0"


async def test_run_state_returns_checkpoint_items_with_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    run_id = "web-state-durable"
    db = tmp_path / "cp.db"
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    await web_server._execute_run(
        run_id, offer="serum X", batch=2, platform="tiktok",
        config_dir="config-mock", db_path=str(db),
        approve_creators=False, edit_concepts=False,
    )
    web_server._runs.pop(run_id)

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(db))

    assert state["phase"] == "done"
    assert state["items"]
    assert all(item["script"] for item in state["items"])
    assert all(item["concept"] for item in state["items"])


async def test_run_state_returns_pending_concepts_during_edit_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    run_id = "web-state-edit"
    db = tmp_path / "cp.db"
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}
    task = asyncio.create_task(
        web_server._execute_run(
            run_id, offer="serum X", batch=2, platform="tiktok",
            config_dir="config-mock", db_path=str(db),
            approve_creators=False, edit_concepts=True,
        )
    )

    try:
        runtime = await _wait_for_run_key(run_id, "concept_edit", task)

        state = await web_server.run_state(run_id, config_dir="config-mock", db=str(db))

        assert state["phase"] == "editing"
        assert len(state["edit_concepts"]) == 2
        assert all(concept["script"] for concept in state["edit_concepts"])

        runtime["concept_edit"].set_result({"concepts": runtime["pending_concepts"]})
        await asyncio.wait_for(task, timeout=8.0)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


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


async def test_execute_run_with_seed_creator_uses_selected_creator(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    run_id = "web-seed-creator"
    db = tmp_path / "cp.db"
    seed = {
        "id": "creator-fixed",
        "image_uri": "data:image/png;base64,SEED",
        "voice_ref": "voice-fixed",
        "voice_preview_uri": "data:audio/wav;base64,SEED",
        "angles": ["front"],
    }
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    await web_server._execute_run(
        run_id, offer="serum X", batch=1, platform="tiktok",
        config_dir="config-mock", db_path=str(db),
        seed_creator=seed,
        approve_creators=False, edit_concepts=False,
    )

    state = await web_server.run_state(run_id, config_dir="config-mock", db=str(db))

    assert state["items"][0]["creator_ref"] == "creator-fixed"


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
