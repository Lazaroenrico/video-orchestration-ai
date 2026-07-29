"""Cobertura dos endpoints e helpers do dashboard web (chamados como coroutines).

Segue o padrão do repo: nada de TestClient — as rotas são coroutines chamadas
diretamente, com asserção em ``HTTPException`` para os caminhos de erro. O estado
global ``web_server._runs`` é limpo por fixture.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import BackgroundTasks, HTTPException
from psycopg_pool import PoolTimeout

from orchestrator import runner
from orchestrator import worker as worker_module
from orchestrator.db.jobs import Job, RunGate
from orchestrator.db.runs import RunSnapshot
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

    event = web_server._runs["r"]["buffer"][0]
    assert event["type"] == "new"  # buffer sempre
    assert event["event_id"] == "local-1"
    assert datetime.fromisoformat(event["occurred_at"]).tzinfo is not None
    assert q.qsize() == 1  # fila cheia: evento descartado sem erro


def test_extract_artifacts_includes_voiceover_between_clips_and_final():
    item = {
        "clips": [{"kind": "clip", "uri": "r2://ugc/clip.mp4"}],
        "voiceover": {
            "kind": "voiceover",
            "uri": "r2://ugc/voiceover.mp3",
        },
        "assembled": {"kind": "video", "uri": "r2://ugc/final.mp4"},
    }

    artifacts = web_server._extract_artifacts(item)

    assert [artifact["kind"] for artifact in artifacts] == [
        "clip",
        "voiceover",
        "video",
    ]


# ------------------------------------------------------------------ #
# runner embutido no web                                             #
# ------------------------------------------------------------------ #

async def test_app_lifespan_starts_embedded_runner_when_flagged(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SharedDatabase:
        async def close(self):
            pass

    async def get_database():
        return SharedDatabase()

    async def close_database():
        pass

    async def runner_loop(wake_event):
        assert wake_event is web_server._web_runner_wake_event
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setenv("ORCH_WEB_EMBEDDED_RUNNER", "true")
    monkeypatch.setattr(web_server, "get_shared_database", get_database)
    monkeypatch.setattr(web_server, "close_shared_database", close_database)
    monkeypatch.setattr(web_server, "_web_embedded_runner_loop", runner_loop)

    async with web_server._app_lifespan(web_server.app):
        await asyncio.wait_for(started.wait(), timeout=1)
        assert web_server._web_runner_wake_event is not None

    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert web_server._web_runner_wake_event is None


async def test_embedded_runner_reuses_shared_database(monkeypatch):
    called = asyncio.Event()
    tenant = object()
    observed: dict[str, object] = {}

    class SharedDatabase:
        async def resolve_tenant(self, _identity):
            return tenant

    shared_database = SharedDatabase()

    async def get_database():
        return shared_database

    async def run_worker_once(**kwargs):
        observed.update(kwargs)
        called.set()
        return False

    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "embedded-runner")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Embedded Runner")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|embedded")
    monkeypatch.setattr(web_server, "get_shared_database", get_database)
    monkeypatch.setattr(web_server, "run_worker_once", run_worker_once)

    wake_event = asyncio.Event()
    task = asyncio.create_task(web_server._web_embedded_runner_loop(wake_event))
    await asyncio.wait_for(called.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert observed["database"] is shared_database
    assert observed["tenant"] is tenant


def test_embedded_runner_poll_interval_falls_back_for_invalid_values(monkeypatch):
    monkeypatch.setenv("ORCH_WEB_RUNNER_POLL_INTERVAL", "not-a-number")

    assert web_server._web_runner_poll_interval() == 2.0


async def test_embedded_runner_survives_job_errors_and_wakes_without_poll_delay(
    monkeypatch,
):
    tenant = object()
    calls = 0
    observed_error = asyncio.Event()
    observed_idle_after_wake = asyncio.Event()

    class SharedDatabase:
        async def resolve_tenant(self, _identity):
            return tenant

    async def get_database():
        return SharedDatabase()

    async def run_worker_once(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        if calls == 2:
            observed_error.set()
            raise RuntimeError("transient worker failure")
        if calls >= 4:
            observed_idle_after_wake.set()
        return False

    monkeypatch.setenv("ORCH_WEB_RUNNER_POLL_INTERVAL", "0.001")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "embedded-errors")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Embedded Errors")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|embedded-errors")
    monkeypatch.setattr(web_server, "get_shared_database", get_database)
    monkeypatch.setattr(web_server, "run_worker_once", run_worker_once)
    wake_event = asyncio.Event()
    wake_event.set()

    task = asyncio.create_task(web_server._web_embedded_runner_loop(wake_event))
    await asyncio.wait_for(observed_error.wait(), timeout=1)
    await asyncio.wait_for(observed_idle_after_wake.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert wake_event.is_set() is False


async def test_embedded_runner_propagates_cancellation_during_a_claim(monkeypatch):
    started = asyncio.Event()

    class SharedDatabase:
        async def resolve_tenant(self, _identity):
            return object()

    async def get_database():
        return SharedDatabase()

    async def run_worker_once(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "embedded-cancel")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Embedded Cancel")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|embedded-cancel")
    monkeypatch.setattr(web_server, "get_shared_database", get_database)
    monkeypatch.setattr(web_server, "run_worker_once", run_worker_once)

    task = asyncio.create_task(
        web_server._web_embedded_runner_loop(asyncio.Event())
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_start_run_defaults_to_staging_config_and_wakes_runner(monkeypatch):
    queued_payloads: list[dict] = []
    wake_event = asyncio.Event()

    class Prompts:
        async def record_last_used(self, **_values):
            pass

    class Jobs:
        async def enqueue_run(self, _run_id, **kwargs):
            queued_payloads.append(kwargs["payload"])
            return SimpleNamespace(job_id=UUID("00000000-0000-0000-0000-000000000011"))

    @asynccontextmanager
    async def open_prompts(_path):
        yield Prompts()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.delenv("ORCH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(web_server, "_web_runner_wake_event", wake_event, raising=False)
    monkeypatch.setattr(web_server.prompt_store, "open_repository", open_prompts)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    response = await web_server.start_run(
        web_server.RunRequest(offer="serum X"),
        BackgroundTasks(),
    )

    assert response["job_id"] == "00000000-0000-0000-0000-000000000011"
    assert queued_payloads[0]["config_dir"] == "config-staging"
    assert wake_event.is_set()


async def test_approve_wakes_embedded_runner_after_persisted_gate_resolution(monkeypatch):
    wake_event = asyncio.Event()
    resolved: list[tuple[UUID, int, dict]] = []

    class Jobs:
        async def resolve_gate(self, gate_id, *, version, resolution):
            resolved.append((gate_id, version, resolution))
            return SimpleNamespace(job_id=UUID("00000000-0000-0000-0000-000000000012"))

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server, "_web_runner_wake_event", wake_event, raising=False)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    response = await web_server.approve(
        "run-1",
        web_server.ApproveRequest(
            gate_id="00000000-0000-0000-0000-000000000001",
            version=3,
            approved=["creator-0"],
        ),
    )

    assert response["job_id"] == "00000000-0000-0000-0000-000000000012"
    assert resolved == [
        (
            UUID("00000000-0000-0000-0000-000000000001"),
            3,
            {"approved": ["creator-0"]},
        )
    ]
    assert wake_event.is_set()


async def test_submit_concepts_wakes_embedded_runner_after_persisted_gate_resolution(monkeypatch):
    wake_event = asyncio.Event()
    resolved: list[tuple[UUID, int, dict]] = []

    class Jobs:
        async def resolve_gate(self, gate_id, *, version, resolution):
            resolved.append((gate_id, version, resolution))
            return SimpleNamespace(job_id=UUID("00000000-0000-0000-0000-000000000013"))

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server, "_web_runner_wake_event", wake_event, raising=False)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    response = await web_server.submit_concepts(
        "run-1",
        web_server.ConceptEditRequest(
            gate_id="00000000-0000-0000-0000-000000000002",
            version=4,
            concepts=[{"id": "concept-0", "script": "edited"}],
        ),
    )

    assert response["job_id"] == "00000000-0000-0000-0000-000000000013"
    assert resolved == [
        (
            UUID("00000000-0000-0000-0000-000000000002"),
            4,
            {"concepts": [{"id": "concept-0", "script": "edited"}]},
        )
    ]
    assert wake_event.is_set()


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


async def test_find_creator_for_draft_recovers_from_media_and_scopes_run(
    tmp_path,
    monkeypatch,
):
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

    creator = await web_server._find_creator_for_draft_repository(
        "creator-0", "web-old"
    )

    assert creator["id"] == "creator-0"
    assert creator["image_uri"] == "/media/web-old/creator-0/image.png"
    assert creator["voice_preview_uri"] == "/media/web-old/creator-0/voice.wav"

    with pytest.raises(HTTPException) as ei:
        await web_server._find_creator_for_draft_repository(
            "creator-0", "web-other"
        )
    assert ei.value.status_code == 404
    assert "web-other" in ei.value.detail


async def test_async_creator_lookup_recovers_local_media_and_reports_scoped_missing(
    tmp_path,
    monkeypatch,
):
    media_root = tmp_path / "media"
    for creator_id in ("creator-0", "creator-1"):
        creator_dir = media_root / "web-old" / creator_id
        creator_dir.mkdir(parents=True)
        (creator_dir / "image.png").write_bytes(b"png")
        (creator_dir / "voice.wav").write_bytes(b"wav")
    monkeypatch.setattr(
        web_server,
        "default_creator_store_path",
        lambda: tmp_path / "missing.json",
    )
    monkeypatch.setattr(web_server, "default_media_path", lambda: media_root)

    creator = await web_server._find_creator_for_draft_repository(
        "creator-1", "web-old"
    )

    assert creator["id"] == "creator-1"
    assert creator["image_uri"] == "/media/web-old/creator-1/image.png"

    with pytest.raises(HTTPException) as exc_info:
        await web_server._find_creator_for_draft_repository(
            "creator-0", "web-other"
        )
    assert exc_info.value.status_code == 404
    assert "web-other" in exc_info.value.detail


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


@pytest.mark.parametrize("endpoint", ["approval", "concepts"])
async def test_legacy_durable_gate_endpoints_translate_stale_ids(
    monkeypatch,
    endpoint,
):
    from orchestrator.db import StaleGateError

    class Jobs:
        async def resolve_gate(self, *_args, **_kwargs):
            raise StaleGateError("stale gate")

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    with pytest.raises(HTTPException) as raised:
        if endpoint == "approval":
            await web_server.approve(
                "run-1",
                web_server.ApproveRequest(
                    approved=[],
                    gate_id="00000000-0000-0000-0000-000000000001",
                    version=1,
                ),
            )
        else:
            await web_server.submit_concepts(
                "run-1",
                web_server.ConceptEditRequest(
                    concepts=[],
                    gate_id="00000000-0000-0000-0000-000000000001",
                    version=1,
                ),
            )

    assert raised.value.status_code == 409


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


async def test_stream_events_resumes_local_replay_after_last_event_id():
    web_server._runs["r"] = {
        "buffer": [
            {"type": "first", "event_id": "local-1"},
            {"type": "second", "event_id": "local-2"},
        ],
        "queues": [],
        "done": True,
    }

    resp = await web_server.stream_events("r", last_event_id="local-1")
    body = "".join([c async for c in resp.body_iterator])

    assert '"type": "first"' not in body
    assert '"type": "second"' in body
    assert "id: local-2" in body
    assert "stream_end" in body


@pytest.mark.parametrize("last_event_id", ["1", "local-not-a-number"])
async def test_stream_events_rejects_invalid_local_last_event_ids(last_event_id):
    web_server._runs["r"] = {"buffer": [], "queues": [], "done": True}

    with pytest.raises(HTTPException) as raised:
        await web_server.stream_events("r", last_event_id=last_event_id)

    assert raised.value.status_code == 400


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


async def test_persisted_stream_reports_database_unavailable_without_traceback(
    monkeypatch,
):
    run_id = "persisted-unavailable"

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id=run_id,
                phase="running",
                state={},
                summary={},
                items=[],
            )

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def unavailable_jobs():
        raise PoolTimeout("secret database host")
        yield

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", unavailable_jobs)
    monkeypatch.setattr(web_server, "_signing_storage", lambda _config: None)

    resp = await web_server.stream_events(run_id)
    body = "".join([chunk async for chunk in resp.body_iterator])

    assert "retry: 30000" in body
    assert '"type": "service_unavailable"' in body
    assert "secret database host" not in body


async def test_persisted_stream_propagates_client_cancellation(monkeypatch):
    run_id = "persisted-cancelled-client"
    polling = asyncio.Event()

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id=run_id,
                phase="running",
                state={},
                summary={},
                items=[],
            )

    class Jobs:
        async def list_events(self, *_args, **_kwargs):
            polling.set()
            await asyncio.Event().wait()

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)
    monkeypatch.setattr(web_server, "_signing_storage", lambda _config: None)

    response = await web_server.stream_events(run_id)
    next_chunk = asyncio.create_task(response.body_iterator.__anext__())
    await asyncio.wait_for(polling.wait(), timeout=1)
    next_chunk.cancel()

    with pytest.raises(asyncio.CancelledError):
        await next_chunk


# ------------------------------------------------------------------ #
# /api/runs e /api/status                                          #
# ------------------------------------------------------------------ #

async def test_list_runs_endpoint_empty_for_missing_db(tmp_path):
    out = await web_server.list_runs_endpoint(db=str(tmp_path / "missing.db"))
    assert out["runs"] == []
    assert isinstance(out["active"], list)
    assert out["errored"] == []
    assert out["cancelled"] == []


async def test_list_runs_endpoint_reports_errored_and_excludes_from_active(tmp_path):
    web_server._runs["running-run"] = {"queues": [], "buffer": [], "done": False}
    web_server._runs["done-run"] = {"queues": [], "buffer": [], "done": True}
    web_server._runs["errored-run"] = {
        "queues": [], "buffer": [], "done": True, "error": "boom",
    }
    web_server._runs["cancelled-run"] = {
        "queues": [],
        "buffer": [],
        "done": True,
        "phase": "cancelled",
        "error": "pipeline_v2_reset",
    }

    out = await web_server.list_runs_endpoint(db=str(tmp_path / "missing.db"))

    # active = só o que está realmente rodando (nem concluído, nem quebrado).
    assert out["active"] == ["running-run"]
    assert out["errored"] == ["errored-run"]
    assert out["cancelled"] == ["cancelled-run"]


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


async def test_run_state_rehydrates_completed_and_active_pipeline_stages(tmp_path):
    run_id = "runtime-progress"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [
            {"type": "run_start", "run_id": run_id, "offer": "serum X", "batch": 2},
            {"type": "node_start", "node": "concepts", "label": "Conceitos"},
            {"type": "node_end", "node": "concepts", "label": "Conceitos"},
            {"type": "node_start", "node": "scripts", "label": "Scripts"},
        ],
        "done": False,
    }

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    stages = {stage["id"]: stage for stage in state["progress"]["stages"]}
    assert state["progress"]["execution_status"] == "running"
    assert stages["concepts"]["status"] == "completed"
    assert stages["scripts"]["status"] == "running"
    assert stages["creator_profiles"]["status"] == "pending"
    assert state["progress"]["active_stage_ids"] == ["scripts"]


async def test_run_state_keeps_parallel_stage_active_until_every_clip_finishes(tmp_path):
    run_id = "runtime-parallel-progress"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [
            {"type": "run_start", "run_id": run_id, "offer": "serum X", "batch": 2},
            {
                "type": "progress_event",
                "operation_id": "video-a",
                "stage_id": "talking_head",
                "node": "ltx",
                "status": "started",
                "item_id": "clip-a",
            },
            {
                "type": "progress_event",
                "operation_id": "video-b",
                "stage_id": "talking_head",
                "node": "ltx",
                "status": "started",
                "item_id": "clip-b",
            },
            {
                "type": "progress_event",
                "operation_id": "video-a",
                "stage_id": "talking_head",
                "node": "ltx",
                "status": "completed",
                "item_id": "clip-a",
            },
        ],
        "done": False,
    }

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    stages = {stage["id"]: stage for stage in state["progress"]["stages"]}
    assert stages["talking_head"]["status"] == "running"
    assert stages["talking_head"]["completed_units"] == 1
    assert stages["talking_head"]["active_units"] == 1
    assert stages["talking_head"]["total_units"] == 2
    assert stages["production"]["status"] == "running"
    assert state["progress"]["active_stage_ids"] == ["talking_head"]


async def test_run_state_returns_semantic_activity_with_server_timestamps(tmp_path):
    run_id = "runtime-activity"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [
            {
                "type": "run_start",
                "run_id": run_id,
                "offer": "serum X",
                "batch": 1,
                "event_id": "local-1",
                "occurred_at": "2026-07-28T10:00:00+00:00",
            },
            {
                "type": "progress_event",
                "operation_id": "scripts-a",
                "stage_id": "scripts",
                "stage_label": "Scripts & review",
                "node": "scripts",
                "status": "started",
                "event_id": "local-2",
                "occurred_at": "2026-07-28T10:01:00+00:00",
            },
        ],
        "done": False,
    }

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    assert state["activity"] == [
        {
            "event_id": "local-1",
            "kind": "run",
            "status": "started",
            "label": "Pipeline started",
            "occurred_at": "2026-07-28T10:00:00+00:00",
            "stage_id": None,
            "item_id": None,
            "attempt": None,
            "detail": None,
        },
        {
            "event_id": "local-2",
            "kind": "stage",
            "status": "started",
            "label": "Scripts & review started",
            "occurred_at": "2026-07-28T10:01:00+00:00",
            "stage_id": "scripts",
            "item_id": None,
            "attempt": None,
            "detail": None,
        },
    ]


async def test_run_state_rehydrates_progress_from_persisted_events_after_restart(
    monkeypatch,
    tmp_path,
):
    run_id = "persisted-progress"
    occurred_at = datetime.fromisoformat("2026-07-28T10:01:00+00:00")

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id=run_id,
                phase="running",
                batch_size=2,
                summary={},
                state={},
                items=[],
            )

    class Jobs:
        async def list_events(self, _run_id):
            return [
                SimpleNamespace(
                    seq=41,
                    event_type="progress_event",
                    data={
                        "operation_id": "scripts-a",
                        "stage_id": "scripts",
                        "stage_label": "Scripts & review",
                        "node": "scripts",
                        "status": "started",
                    },
                    created_at=occurred_at,
                )
            ]

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    async def no_checkpoint(*_args, **_kwargs):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.runner, "get_status", no_checkpoint)
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    assert state["progress"]["active_stage_ids"] == ["scripts"]
    assert state["activity"][0]["event_id"] == "41"
    assert state["activity"][0]["occurred_at"] == occurred_at.isoformat()


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
    assert state["gate"] is None


async def test_run_state_returns_versioned_persisted_concept_gate(monkeypatch, tmp_path):
    run_id = "persisted-edit"
    gate = RunGate(
        gate_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=run_id,
        gate_type="edit_concepts",
        version=2,
        status="pending",
        payload={"concepts": [{"id": "concept-a", "script": "draft"}]},
        resolution=None,
    )

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id=run_id,
                phase="editing",
                state={},
                summary={},
                items=[],
            )

    class Jobs:
        async def get_pending_gate(self, _run_id):
            return gate

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    async def no_checkpoint(*_args, **_kwargs):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.runner, "get_status", no_checkpoint)
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    assert state["phase"] == "editing"
    assert state["edit_concepts"] == [{"id": "concept-a", "script": "draft"}]
    assert state["gate"] == {
        "gate_id": str(gate.gate_id),
        "version": 2,
        "gate_type": "edit_concepts",
    }


async def test_run_state_ignores_a_persisted_gate_with_the_wrong_phase_type(
    monkeypatch,
    tmp_path,
):
    run_id = "persisted-mismatched-gate"
    gate = RunGate(
        gate_id=UUID("00000000-0000-0000-0000-000000000011"),
        run_id=run_id,
        gate_type="review_creative_plan",
        version=1,
        status="pending",
        payload={"concepts": [{"id": "wrong-source"}], "creators": []},
        resolution=None,
    )

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id=run_id,
                phase="editing",
                state={"pending_concepts": [{"id": "canonical-source"}]},
                summary={},
                items=[],
            )

    class Jobs:
        async def get_pending_gate(self, _run_id):
            return gate

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    async def no_checkpoint(*_args, **_kwargs):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.runner, "get_status", no_checkpoint)
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    assert state["gate"] is None
    assert state["edit_concepts"] == [{"id": "canonical-source"}]


async def test_run_state_exposes_the_legacy_local_concept_edit_payload(tmp_path):
    run_id = "runtime-editing"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [],
        "done": False,
        "concept_edit": asyncio.get_running_loop().create_future(),
        "pending_concepts": [{"id": "concept-a", "script": "draft"}],
    }

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    assert state["phase"] == "editing"
    assert state["edit_concepts"] == [{"id": "concept-a", "script": "draft"}]


async def test_run_state_returns_versioned_persisted_creator_gate(monkeypatch, tmp_path):
    run_id = "persisted-awaiting"
    gate = RunGate(
        gate_id=UUID("00000000-0000-0000-0000-000000000002"),
        run_id=run_id,
        gate_type="approve_creators",
        version=1,
        status="pending",
        payload={
            "creators": [{
                "creator_id": "creator-0",
                "image": "/media/persisted-awaiting/creator-0/image.png",
                "voice": "/media/persisted-awaiting/creator-0/voice.wav",
            }]
        },
        resolution=None,
    )

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id=run_id,
                phase="awaiting",
                state={},
                summary={},
                items=[],
            )

    class Jobs:
        async def get_pending_gate(self, _run_id):
            return gate

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    async def no_checkpoint(*_args, **_kwargs):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.runner, "get_status", no_checkpoint)
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    assert state["phase"] == "awaiting"
    assert state["awaiting"][0]["id"] == "creator-0"
    assert state["gate"] == {
        "gate_id": str(gate.gate_id),
        "version": 1,
        "gate_type": "approve_creators",
    }


async def test_run_state_normalizes_creator_image_in_persisted_review_gate(
    monkeypatch,
    tmp_path,
):
    run_id = "persisted-review"
    gate = RunGate(
        gate_id=UUID("00000000-0000-0000-0000-000000000003"),
        run_id=run_id,
        gate_type="review_creative_plan",
        version=1,
        status="pending",
        payload={
            "type": "review_creative_plan",
            "concepts": [{"id": "concept-a", "script": "draft"}],
            "creators": [
                {
                    "id": "creator-0",
                    "upscaled_base": "r2://ugc/persisted-review/creator-0/image.png",
                    "voice_preview_uri": (
                        "r2://ugc/persisted-review/creator-0/voice.mp3"
                    ),
                    "archetype": "Expert",
                },
                {
                    "id": "creator-1",
                    "upscaled_base": "r2://ugc/persisted-review/creator-1/image.png",
                    "voice_preview_uri": (
                        "r2://ugc/persisted-review/creator-1/voice.mp3"
                    ),
                    "archetype": "Customer",
                },
            ],
        },
        resolution=None,
    )

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id=run_id,
                phase="review",
                state={},
                summary={},
                items=[],
            )

    class Jobs:
        async def get_pending_gate(self, _run_id):
            return gate

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    async def no_checkpoint(*_args, **_kwargs):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.runner, "get_status", no_checkpoint)
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    state = await web_server.run_state(
        run_id,
        config_dir="config-mock",
        db=str(tmp_path / "cp.db"),
    )

    creator = state["review"]["creators"][0]
    assert creator["image_uri"] == (
        "r2://ugc/persisted-review/creator-0/image.png"
    )
    assert creator["image"] == creator["image_uri"]
    assert creator["archetype"] == "Expert"


def test_persisted_review_event_normalizes_internal_creator_image() -> None:
    event = SimpleNamespace(
        seq=12,
        event_type="awaiting_review",
        data={
            "run_id": "persisted-review",
            "concepts": [],
            "creators": [
                {
                    "id": "creator-0",
                    "upscaled_base": "r2://ugc/run/creator-0/image.png",
                }
            ],
        },
        created_at=datetime.fromisoformat("2026-07-29T10:00:00+00:00"),
    )

    payload = web_server._persisted_event_payload(event)

    assert payload["creators"][0]["image_uri"] == (
        "r2://ugc/run/creator-0/image.png"
    )
    assert "upscaled_base" not in payload["creators"][0]


def test_worker_review_gate_uses_public_creator_media_contract() -> None:
    payload = worker_module._public_gate_payload(
        {
            "type": "review_creative_plan",
            "concepts": [],
            "creators": [
                {
                    "id": "creator-0",
                    "upscaled_base": "r2://ugc/run/creator-0/image.png",
                    "voice_id": "r2://ugc/run/creator-0/voice.mp3",
                    "archetype": "Expert",
                }
            ],
        }
    )

    assert payload["creators"][0]["image_uri"] == (
        "r2://ugc/run/creator-0/image.png"
    )
    assert payload["creators"][0]["image"] == payload["creators"][0]["image_uri"]
    assert payload["creators"][0]["archetype"] == "Expert"
    assert "upscaled_base" not in payload["creators"][0]


async def test_retry_failed_persisted_run_creates_clean_fork(monkeypatch):
    old_run_id = "web-failed"
    wake_event = asyncio.Event()
    original_payload = {
        "offer": "serum X",
        "batch": 2,
        "platform": "tiktok",
        "config_dir": "config-mock",
        "db_path": "/tmp/orchestrator.db",
        "creator_prompt": "creator prompt",
        "video_prompt": "video prompt",
        "approve_creators": False,
        "edit_concepts": True,
        "seed_creator": {"id": "creator-fixed"},
    }
    enqueued: list[dict[str, object]] = []

    class Runs:
        async def get(self, run_id):
            assert run_id == old_run_id
            return RunSnapshot(
                run_id=old_run_id,
                phase="error",
                offer="serum X",
                platform="tiktok",
                batch_size=2,
                error="adapter failed",
                state={"partial": True},
                summary={},
                items=[{"id": "old-item"}],
            )

    class Jobs:
        async def get_initial_run_payload(self, run_id):
            assert run_id == old_run_id
            return original_payload

        async def enqueue_run(self, run_id, **kwargs):
            enqueued.append({"run_id": run_id, **kwargs})
            return Job(
                job_id=UUID("00000000-0000-0000-0000-000000000101"),
                run_id=run_id,
                kind="execute_run",
                status="queued",
                payload=kwargs["payload"],
                attempt=0,
                max_attempts=kwargs.get("max_attempts", 5),
                available_at=kwargs.get("now"),
                lease_expires_at=None,
                worker_id=None,
                error=None,
            )

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server, "_web_runner_wake_event", wake_event, raising=False)
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    response = await web_server.retry_run(old_run_id)

    assert response["run_id"] != old_run_id
    assert response["run_id"].startswith("web-")
    assert response["source_run_id"] == old_run_id
    assert response["job_id"] == "00000000-0000-0000-0000-000000000101"
    assert enqueued == [{
        "run_id": response["run_id"],
        "offer": "serum X",
        "platform": "tiktok",
        "batch_size": 2,
        "payload": {**original_payload, "source_run_id": old_run_id},
    }]
    assert wake_event.is_set()


async def test_retry_unknown_persisted_run_returns_404(monkeypatch):
    class Runs:
        async def get(self, _run_id):
            return None

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)

    with pytest.raises(HTTPException) as exc:
        await web_server.retry_run("missing-run")

    assert exc.value.status_code == 404


async def test_retry_non_failed_persisted_run_returns_409(monkeypatch):
    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id="web-running",
                phase="running",
                offer="serum X",
                platform="tiktok",
                batch_size=2,
                summary={},
                state={},
                items=[],
            )

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)

    with pytest.raises(HTTPException) as exc:
        await web_server.retry_run("web-running")

    assert exc.value.status_code == 409


async def test_retry_failed_run_without_initial_payload_returns_409(monkeypatch):
    enqueued: list[str] = []

    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id="web-no-payload",
                phase="error",
                offer="serum X",
                platform="tiktok",
                batch_size=2,
                error="adapter failed",
                summary={},
                state={},
                items=[],
            )

    class Jobs:
        async def get_initial_run_payload(self, _run_id):
            return None

        async def enqueue_run(self, run_id, **_kwargs):
            enqueued.append(run_id)

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def open_jobs():
        yield Jobs()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", open_jobs)

    with pytest.raises(HTTPException) as exc:
        await web_server.retry_run("web-no-payload")

    assert exc.value.status_code == 409
    assert enqueued == []


def test_retry_payload_rejects_missing_server_owned_fields():
    with pytest.raises(HTTPException) as raised:
        web_server._retry_payload_fields(
            {"offer": "", "platform": "tiktok", "batch": True}
        )

    assert raised.value.status_code == 409


async def test_retry_requires_the_durable_job_repository(monkeypatch):
    class Runs:
        async def get(self, _run_id):
            return RunSnapshot(
                run_id="web-no-jobs",
                phase="error",
                offer="serum X",
                platform="tiktok",
                batch_size=1,
                error="adapter failed",
                summary={},
                state={},
                items=[],
            )

    @asynccontextmanager
    async def open_runs():
        yield Runs()

    @asynccontextmanager
    async def no_jobs():
        yield None

    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    monkeypatch.setattr(web_server.job_store, "open_repository", no_jobs)

    with pytest.raises(HTTPException) as raised:
        await web_server.retry_run("web-no-jobs")

    assert raised.value.status_code == 409


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


async def test_run_state_returns_combined_pending_creative_review(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    run_id = "web-state-edit"
    db = tmp_path / "cp.db"
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}
    task = asyncio.create_task(
        web_server._execute_run(
            run_id, offer="serum X", batch=2, platform="tiktok",
            config_dir="config-mock", db_path=str(db),
            approve_creators=False, edit_concepts=False, review_plan=True,
        )
    )

    try:
        runtime = await _wait_for_run_key(run_id, "review", task)

        state = await web_server.run_state(run_id, config_dir="config-mock", db=str(db))

        assert state["phase"] == "review"
        assert state["edit_concepts"] == []
        assert len(state["review"]["concepts"]) == 2
        assert len(state["review"]["creators"]) == 2
        assert all(concept["script"] for concept in state["review"]["concepts"])

        runtime["review"].set_result({"action": "approve"})
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


async def test_execute_run_rejects_an_unknown_human_gate(monkeypatch, tmp_path):
    class Graph:
        async def astream_events(self, *_args, **_kwargs):
            if False:
                yield None

        async def aget_state(self, _config):
            interrupt = SimpleNamespace(value={"type": "legacy-unknown"})
            task = SimpleNamespace(interrupts=[interrupt])
            return SimpleNamespace(
                next=("legacy",),
                tasks=[task],
                values={},
            )

    @asynccontextmanager
    async def open_fake_checkpointer(_path):
        yield object()

    monkeypatch.setattr(web_server, "load_pipeline", lambda _path: {"batch": {}})
    monkeypatch.setattr(web_server, "load_providers", lambda _path: {})
    monkeypatch.setattr(web_server, "load_agent_catalog", lambda _path: object())
    monkeypatch.setattr(
        web_server,
        "build_adapter_from_providers",
        lambda *_args: object(),
    )
    monkeypatch.setattr(web_server, "run_trace_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(web_server, "open_checkpointer", open_fake_checkpointer)
    monkeypatch.setattr(web_server, "build_graph", lambda *_args, **_kwargs: Graph())
    web_server._runs["unknown-gate"] = {
        "queues": [],
        "buffer": [],
        "done": False,
    }

    await web_server._execute_run(
        "unknown-gate",
        offer="Serum X",
        batch=1,
        platform="tiktok",
        config_dir="config-mock",
        db_path=str(tmp_path / "cp.db"),
        _run_repository=None,
    )

    errors = [
        event
        for event in web_server._runs["unknown-gate"]["buffer"]
        if event.get("type") == "error"
    ]
    assert "unsupported human gate" in errors[-1]["message"]


async def test_local_start_signs_seed_and_can_persist_run_index(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ORCH_PROMPTS", str(tmp_path / "prompts.json"))
    canonical = {
        "id": "creator-0",
        "image_uri": "r2://bucket/creator-0.webp",
    }
    signed = {
        "id": "creator-0",
        "image_uri": "https://signed.test/creator-0.webp",
    }

    async def find_creator(*_args, **_kwargs):
        return canonical

    async def sign_payload(payload, _config_dir):
        assert payload == canonical
        return signed

    class RecordingRuns:
        async def start(self, run_id, **metadata):
            self.started = (run_id, metadata)

    runs = RecordingRuns()

    @asynccontextmanager
    async def open_runs():
        yield runs

    monkeypatch.setattr(
        web_server,
        "_find_creator_for_draft_repository",
        find_creator,
    )
    monkeypatch.setattr(web_server, "_sign_payload", sign_payload)
    monkeypatch.setattr(web_server.run_store, "open_repository", open_runs)
    background = BackgroundTasks()

    response = await web_server.start_run(
        web_server.RunRequest(
            creator_id="creator-0",
            approve_creators=False,
            edit_concepts=False,
        ),
        background,
    )

    assert runs.started[0] == response["run_id"]
    assert background.tasks[0].args[-1] == signed


async def test_local_execute_persists_combined_review_completion_and_error(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    phases = []

    class RecordingRuns:
        async def save(self, _run_id, *, phase, **_payload):
            phases.append(phase)

    run_id = "local-persisted-gates"
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}
    task = asyncio.create_task(
        web_server._execute_run(
            run_id,
            offer="serum X",
            batch=1,
            platform="tiktok",
            config_dir="config-mock",
            db_path=str(tmp_path / "gates.sqlite"),
            approve_creators=True,
            edit_concepts=True,
            _run_repository=RecordingRuns(),
        )
    )
    runtime = await _wait_for_run_key(run_id, "review", task)
    runtime["review"].set_result({"action": "approve"})
    await asyncio.wait_for(task, timeout=8)

    original_load_pipeline = web_server.load_pipeline

    def broken_config(*_args, **_kwargs):
        raise RuntimeError("broken local config")

    monkeypatch.setattr(web_server, "load_pipeline", broken_config)
    web_server._runs["local-persisted-error"] = {
        "queues": [],
        "buffer": [],
        "done": False,
    }
    await web_server._execute_run(
        "local-persisted-error",
        offer="serum X",
        batch=1,
        platform="tiktok",
        config_dir=None,
        db_path=str(tmp_path / "error.sqlite"),
        approve_creators=False,
        edit_concepts=False,
        _run_repository=RecordingRuns(),
    )
    monkeypatch.setattr(web_server, "load_pipeline", original_load_pipeline)

    assert phases[0] == "review"
    assert "editing" not in phases
    assert "awaiting" not in phases
    assert "running" in phases
    assert phases[-2:] == ["done", "error"]
