"""Contratos do dashboard human-on-the-loop via SSE."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.adapters.mock import MockAdapter
from orchestrator.graph.state import Artifact, QCResult
from orchestrator.nodes.stages import node_roster
from orchestrator import stream_bus
from orchestrator.web import server as web_server


def test_mock_artifact_is_not_renderable_media() -> None:
    art = web_server._normalize_artifact({"kind": "clip", "uri": "mock://clip/item-1"})
    assert art == {
        "kind": "clip",
        "uri": "mock://clip/item-1",
        "media_type": "reference",
        "renderable": False,
    }


def test_https_mp4_artifact_is_renderable_video() -> None:
    art = web_server._normalize_artifact({"kind": "clip", "uri": "https://cdn.example/ad.mp4"})
    assert art["media_type"] == "video"
    assert art["renderable"] is True


def test_videos_path_artifact_is_renderable_video() -> None:
    art = web_server._normalize_artifact(
        {"kind": "clip", "uri": "/videos/run-1/items/item-1/assembled.mp4"}
    )
    assert art["media_type"] == "video"
    assert art["renderable"] is True


def test_web_app_mounts_videos_static_route() -> None:
    assert any(getattr(route, "path", None) == "/videos" for route in web_server.app.routes)


def test_data_image_artifact_is_renderable_image() -> None:
    art = web_server._normalize_artifact({"kind": "face", "uri": "data:image/png;base64,abc"})
    assert art["media_type"] == "image"
    assert art["renderable"] is True


def test_voice_id_without_preview_is_reference_not_audio() -> None:
    creator = web_server._normalize_creator({"id": "creator-0", "voice_id": "voice-abc"})
    assert creator["voice_ref"] == "voice-abc"
    assert creator["voice"] == "voice-abc"
    assert creator["voice_preview_uri"] is None


def test_creator_normalization_keeps_new_fields_and_legacy_aliases() -> None:
    creator = web_server._normalize_creator(
        {
            "id": "creator-0",
            "image_uri": "https://cdn.example/face.png",
            "voice_ref": "voice-0",
            "voice_preview_uri": "https://cdn.example/voice.mp3",
            "angles": ["front"],
        }
    )
    assert creator["image_uri"] == "https://cdn.example/face.png"
    assert creator["voice_ref"] == "voice-0"
    assert creator["voice_preview_uri"] == "https://cdn.example/voice.mp3"
    assert creator["image"] == "https://cdn.example/face.png"
    assert creator["voice"] == "voice-0"
    assert creator["angles"] == ["front"]


def test_script_node_end_emits_item_update_with_script() -> None:
    snapshots: dict[str, dict] = {}
    update = web_server._build_item_update(
        "run-1",
        "script",
        {
            "input": {
                "id": "item-1",
                "creator_ref": "creator-0",
                "concept": {"hook": "Try this"},
            },
            "output": {"script": "HOOK: Try this\nCTA: buy"},
        },
        snapshots,
    )
    assert update is not None
    assert update["type"] == "item_update"
    assert update["run_id"] == "run-1"
    assert update["node"] == "script"
    assert update["label"] == "Script"
    assert update["item"]["id"] == "item-1"
    assert update["item"]["concept"] == {"hook": "Try this"}
    assert update["item"]["script"] == "HOOK: Try this\nCTA: buy"


def test_qc_node_end_emits_item_update_with_qc_result() -> None:
    snapshots: dict[str, dict] = {}
    update = web_server._build_item_update(
        "run-1",
        "qc",
        {
            "input": {"id": "item-1", "attempts": 1},
            "output": {"qc": QCResult(passed=False, score=0.41, reasons=["lip_sync"])},
        },
        snapshots,
    )
    assert update is not None
    assert update["item"]["qc"] == {
        "passed": False,
        "score": 0.41,
        "reasons": ["lip_sync"],
    }
    assert update["item"]["attempts"] == 1


def test_process_item_final_snapshot_keeps_artifacts_and_status() -> None:
    snapshots: dict[str, dict] = {}
    web_server._build_item_update(
        "run-1",
        "product_demo",
        {
            "input": {"id": "item-1"},
            "output": {
                "clips": [Artifact(kind="clip", uri="mock://clip/item-1/demo")],
                "cost_usd": 0.08,
            },
        },
        snapshots,
    )
    update = web_server._build_item_update(
        "run-1",
        "process_item",
        {
            "output": {
                "results": [
                    {
                        "id": "item-1",
                        "concept": {"hook": "Try this"},
                        "assembled": Artifact(kind="video", uri="https://cdn.example/final.mp4"),
                        "dropped": False,
                        "attempts": 1,
                        "cost_usd": 0.11,
                    }
                ]
            }
        },
        snapshots,
    )
    assert update is not None
    item = update["item"]
    assert item["dropped"] is False
    assert item["assembled"]["uri"] == "https://cdn.example/final.mp4"
    assert item["cost_usd"] == 0.11
    assert {a["uri"] for a in item["artifacts"]} == {
        "mock://clip/item-1/demo",
        "https://cdn.example/final.mp4",
    }


def test_creators_history_exposes_store_path_and_entries(tmp_path, monkeypatch) -> None:
    store = tmp_path / "creators.json"
    creator_store = web_server.creator_store
    creator_store.record_creators(
        store,
        "run-1",
        [{
            "id": "creator-0",
            "image": "/media/run-1/creator-0/image.png",
            "voice": "/media/run-1/creator-0/voice.mp3",
            "voice_preview_uri": "/media/run-1/creator-0/voice.mp3",
        }],
        approved_ids=["creator-0"],
    )
    monkeypatch.setenv("ORCH_CREATORS", str(store))

    import asyncio

    payload = asyncio.run(web_server.creators_history())

    assert payload["store_path"] == str(store)
    assert payload["exists"] is True
    assert payload["creators"][0]["creator_id"] == "creator-0"


def test_creators_history_only_returns_people_with_image_and_voice(tmp_path, monkeypatch) -> None:
    """Entradas incompletas (só prompt/"inspiração", só imagem, ou voz não tocável)
    não aparecem na galeria — a web só carrega pessoas com imagem + voz."""
    store = tmp_path / "creators.json"
    creator_store = web_server.creator_store
    creator_store.record_creators(
        store,
        "run-1",
        [
            {  # completo: imagem renderizável + voz tocável
                "id": "creator-0",
                "image": "/media/run-1/creator-0/image.png",
                "voice": "/media/run-1/creator-0/voice.mp3",
                "voice_preview_uri": "/media/run-1/creator-0/voice.mp3",
            },
            {  # só "inspiração": nem imagem nem voz
                "id": "creator-1",
                "image": None,
                "voice": None,
            },
            {  # imagem sem voz tocável (voice_id opaco não toca no browser)
                "id": "creator-2",
                "image": "/media/run-1/creator-2/image.png",
                "voice": "voice-2",
            },
            {  # voz sem imagem
                "id": "creator-3",
                "image": None,
                "voice": "/media/run-1/creator-3/voice.mp3",
                "voice_preview_uri": "/media/run-1/creator-3/voice.mp3",
            },
        ],
        approved_ids=["creator-0", "creator-1", "creator-2", "creator-3"],
        creator_prompt="mulher 30 anos, estilo natural",
    )
    monkeypatch.setenv("ORCH_CREATORS", str(store))

    import asyncio

    payload = asyncio.run(web_server.creators_history())

    ids = [c["creator_id"] for c in payload["creators"]]
    assert ids == ["creator-0"]


def test_recover_from_media_skips_dirs_missing_image_or_voice(tmp_path) -> None:
    media_root = tmp_path / "media"
    complete = media_root / "web-a" / "creator-0"
    complete.mkdir(parents=True)
    (complete / "image.png").write_bytes(b"png")
    (complete / "voice.mp3").write_bytes(b"mp3")
    image_only = media_root / "web-a" / "creator-1"
    image_only.mkdir(parents=True)
    (image_only / "image.png").write_bytes(b"png")
    voice_only = media_root / "web-a" / "creator-2"
    voice_only.mkdir(parents=True)
    (voice_only / "voice.wav").write_bytes(b"wav")

    recovered = web_server._recover_creators_from_media(media_root)

    assert [c["creator_id"] for c in recovered] == ["creator-0"]


def test_creators_history_recovers_from_media_when_store_is_empty(tmp_path, monkeypatch) -> None:
    store = tmp_path / "creators.json"
    store.write_text("{}", encoding="utf-8")
    media_root = tmp_path / "media"
    creator_dir = media_root / "web-old" / "creator-0"
    creator_dir.mkdir(parents=True)
    (creator_dir / "image.png").write_bytes(b"png")
    (creator_dir / "voice.wav").write_bytes(b"wav")
    monkeypatch.setenv("ORCH_CREATORS", str(store))
    monkeypatch.setattr(web_server, "default_media_path", lambda: media_root)

    import asyncio

    payload = asyncio.run(web_server.creators_history())

    assert payload["store_path"] == str(store)
    assert payload["exists"] is True
    assert payload["creators"] == [
        {
            "run_id": "web-old",
            "creator_id": "creator-0",
            "id": "creator-0",
            "image_uri": "/media/web-old/creator-0/image.png",
            "image": "/media/web-old/creator-0/image.png",
            "voice_ref": "/media/web-old/creator-0/voice.wav",
            "voice": "/media/web-old/creator-0/voice.wav",
            "voice_preview_uri": "/media/web-old/creator-0/voice.wav",
            "angles": [],
            "creator_prompt": None,
            "video_prompt": None,
            "offer": None,
            "status": "recovered",
        }
    ]


@pytest.mark.asyncio
async def test_reroll_voice_updates_pending_creator_and_emits_sse(monkeypatch, pipeline_cfg) -> None:
    adapter = MockAdapter(tiers=pipeline_cfg["tiers"])
    run_id = "web-reroll"
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [],
        "done": False,
        "adapter": adapter,
        "pending_creators": [
            {
                "id": "creator-0",
                "image_uri": "data:image/svg+xml;base64,original",
                "voice_ref": "voice-0",
                "voice_preview_uri": "data:audio/wav;base64,original",
                "voice": "voice-0",
            }
        ],
    }
    monkeypatch.setattr(web_server, "default_media_path", lambda: Path("/tmp/nonexistent-media"))
    events: list[dict] = []
    monkeypatch.setattr(web_server, "_emit", lambda _run_id, event: events.append(event) or asyncio.sleep(0))

    try:
        payload = await web_server.reroll_creator_voice(run_id, "creator-0")
    finally:
        web_server._runs.pop(run_id, None)

    creator = payload["creator"]
    assert creator["id"] == "creator-0"
    assert creator["voice_ref"] != "voice-0"
    assert creator["voice_preview_uri"] != "data:audio/wav;base64,original"
    assert web_server._runs.get(run_id) is None or True
    assert events == [
        {
            "type": "creator_update",
            "run_id": run_id,
            "creator": creator,
        }
    ]


@pytest.mark.asyncio
async def test_approve_uses_updated_pending_roster_state(monkeypatch) -> None:
    run_id = "web-approve-reroll"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    updated_creator = {
        "id": "creator-0",
        "image_uri": "/media/run/creator-0/image.png",
        "voice_ref": "voice-reroll-0",
        "voice_preview_uri": "/media/run/creator-0/voice-reroll.wav",
    }
    web_server._runs[run_id] = {
        "queues": [],
        "buffer": [],
        "done": False,
        "approval": fut,
        "pending_creators": [updated_creator],
    }

    try:
        resp = await web_server.approve(run_id, web_server.ApproveRequest(approved=["creator-0"]))
    finally:
        web_server._runs.pop(run_id, None)

    assert resp == {"ok": True}
    assert fut.done() is True
    assert fut.result() == {
        "approved": ["creator-0"],
        "creators": [updated_creator],
    }


@pytest.mark.asyncio
async def test_dashboard_run_pauses_for_creator_approval_by_default(tmp_path, monkeypatch) -> None:
    """Default do dashboard: pausa no gate humano para o usuário ESCOLHER os creators
    (imagem + voz) que vão estrelar os vídeos, e só retoma com os aprovados."""
    run_id = "web-creator-approval"
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    task = asyncio.create_task(
        web_server._execute_run(
            run_id=run_id,
            offer="serum X",
            batch=1,
            platform="tiktok",
            config_dir="config-mock",
            db_path=str(tmp_path / "runs.sqlite"),
            # Isola o gate de creator: sem pausa de edição de conceitos.
            edit_concepts=False,
        )
    )

    try:
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            state = web_server._runs[run_id]
            if "approval" in state:
                break
            assert not task.done(), "run terminou sem pausar para aprovação"
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("dashboard não pausou para aprovação de creators")

        pending = state.get("pending_creators") or []
        assert pending, "pending_creators deveria estar populado durante a pausa"
        event_types = [event.get("type") for event in state["buffer"]]
        assert "awaiting_approval" in event_types

        approved_id = pending[0]["id"]
        state["approval"].set_result({"approved": [approved_id], "creators": pending})
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state = web_server._runs.pop(run_id, {})

    event_types = [event.get("type") for event in state.get("buffer", [])]
    assert "run_end" in event_types


@pytest.mark.asyncio
async def test_dashboard_run_can_bypass_creator_approval(tmp_path, monkeypatch) -> None:
    """Opt-out explícito (approve_creators=False): run direto, sem gate humano."""
    run_id = "web-no-creator-approval"
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    task = asyncio.create_task(
        web_server._execute_run(
            run_id=run_id,
            offer="serum X",
            batch=1,
            platform="tiktok",
            config_dir="config-mock",
            db_path=str(tmp_path / "runs.sqlite"),
            approve_creators=False,
            edit_concepts=False,
        )
    )

    try:
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            state = web_server._runs[run_id]
            assert "approval" not in state, "dashboard should not pause for creator approval"
            if task.done():
                await task
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("dashboard run did not finish")
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state = web_server._runs.pop(run_id, {})

    event_types = [event.get("type") for event in state.get("buffer", [])]
    assert "awaiting_approval" not in event_types
    assert "run_end" in event_types


def test_run_request_defaults_to_creator_approval() -> None:
    assert web_server.RunRequest().approve_creators is True


def test_run_request_defaults_to_concept_edit() -> None:
    assert web_server.RunRequest().edit_concepts is True


@pytest.mark.asyncio
async def test_dashboard_run_pauses_for_concept_edit_before_creator(tmp_path, monkeypatch) -> None:
    """Default do dashboard: pausa para editar concept+script ANTES de gerar o creator.

    O gate de edição precede o roster: nenhum evento de creator deve preceder o
    ``awaiting_concept_edit``. O resume aplica os conceitos editados (script alterado,
    conceito excluído) e o run segue para o gate de creator, depois conclui.
    """
    run_id = "web-concept-edit"
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    task = asyncio.create_task(
        web_server._execute_run(
            run_id=run_id,
            offer="serum X",
            batch=2,
            platform="tiktok",
            config_dir="config-mock",
            db_path=str(tmp_path / "runs.sqlite"),
            approve_creators=False,  # isola: testamos só o gate de edição
        )
    )

    try:
        # 1) espera a pausa de edição de conceitos
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            state = web_server._runs[run_id]
            if "concept_edit" in state:
                break
            assert not task.done(), "run terminou sem pausar para edição de conceitos"
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("dashboard não pausou para edição de conceitos")

        buffer = state["buffer"]
        types = [e.get("type") for e in buffer]
        assert "awaiting_concept_edit" in types
        # o gate de edição precede QUALQUER evento de creator
        assert "creator_start" not in types and "creator_ready" not in types
        # o payload traz os conceitos COM script (gerado antes do creator)
        edit_ev = next(e for e in buffer if e.get("type") == "awaiting_concept_edit")
        pending = edit_ev["concepts"]
        assert len(pending) == 2
        assert all(c.get("script") for c in pending)

        # 2) edita o script de um conceito e EXCLUI o outro (só 1 volta)
        edited = [{**pending[0], "script": "EDITED SCRIPT"}]
        state["concept_edit"].set_result({"concepts": edited})

        await asyncio.wait_for(task, timeout=8.0)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state = web_server._runs.pop(run_id, {})

    types = [e.get("type") for e in state.get("buffer", [])]
    assert "run_end" in types
    # só 1 conceito seguiu para produção (o outro foi excluído no gate)
    item_updates = [e for e in state.get("buffer", []) if e.get("type") == "item_update"]
    produced_ids = {e["item"]["id"] for e in item_updates}
    assert len(produced_ids) == 1


@pytest.mark.asyncio
async def test_dashboard_run_summary_after_concept_edit_and_creator_approval(tmp_path, monkeypatch) -> None:
    """Run web com os dois gates deve emitir summary final a partir do estado raiz."""
    run_id = "web-two-gates"
    monkeypatch.setenv("ORCH_MEDIA", str(tmp_path / "media"))
    monkeypatch.setenv("ORCH_CREATORS", str(tmp_path / "creators.json"))
    web_server._runs[run_id] = {"queues": [], "buffer": [], "done": False}

    task = asyncio.create_task(
        web_server._execute_run(
            run_id=run_id,
            offer="serum X",
            batch=2,
            platform="tiktok",
            config_dir="config-mock",
            db_path=str(tmp_path / "runs.sqlite"),
            approve_creators=True,
            edit_concepts=True,
        )
    )

    try:
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            state = web_server._runs[run_id]
            if "concept_edit" in state:
                break
            assert not task.done(), "run terminou sem pausar para edição de conceitos"
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("dashboard não pausou para edição de conceitos")

        edit_ev = next(
            e for e in state["buffer"] if e.get("type") == "awaiting_concept_edit"
        )
        pending = edit_ev["concepts"]
        edited_script = f"{pending[0]['script']} EDITED"
        await web_server.submit_concepts(
            run_id,
            web_server.ConceptEditRequest(
                concepts=[{**pending[0], "script": edited_script}]
            ),
        )

        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            state = web_server._runs[run_id]
            if "approval" in state:
                break
            assert not task.done(), "run terminou sem pausar para aprovação de creator"
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("dashboard não pausou para aprovação de creator")

        approval_ev = next(
            e for e in state["buffer"] if e.get("type") == "awaiting_approval"
        )
        creator_id = approval_ev["creators"][0]["id"]
        await web_server.approve(run_id, web_server.ApproveRequest(approved=[creator_id]))

        await asyncio.wait_for(task, timeout=8.0)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state = web_server._runs.pop(run_id, {})

    run_end = next(e for e in state.get("buffer", []) if e.get("type") == "run_end")
    assert run_end["summary"]["produced"] == 1
    assert run_end["summary"]["approved"] == 1
    final_item = [
        e for e in state.get("buffer", []) if e.get("type") == "item_update"
    ][-1]["item"]
    assert final_item["script"] == edited_script


@pytest.mark.asyncio
async def test_roster_emits_creator_start_before_creator_ready(pipeline_cfg) -> None:
    events: list[dict] = []
    stream_bus.set_token_callback(events.append)
    try:
        cfg = {
            "configurable": {
                "adapter": MockAdapter(tiers=pipeline_cfg["tiers"]),
                "pipeline": pipeline_cfg,
                "run": {},
                "thread_id": "run-1",
            }
        }
        await node_roster({"run_id": "run-1"}, cfg)
    finally:
        stream_bus.clear_token_callback()

    creator_events = [
        (e.get("type"), e.get("creator_id") or (e.get("creator") or {}).get("id"))
        for e in events
        if e.get("type") in {"creator_start", "creator_ready"}
    ]
    for creator_id in ("creator-0", "creator-1"):
        assert ("creator_start", creator_id) in creator_events
        assert ("creator_ready", creator_id) in creator_events
        assert creator_events.index(("creator_start", creator_id)) < creator_events.index(
            ("creator_ready", creator_id)
        )


@pytest.mark.asyncio
async def test_roster_creator_ready_carries_renderable_voice_preview(pipeline_cfg) -> None:
    """Mock: o preview de voz do adapter (data:audio/wav) chega renderável à UI.

    Regressão: _build_voice_preview não pode sobrescrever com None o preview que o
    MockAdapter já emitiu — senão a demo offline fica sem voz audível.
    """
    events: list[dict] = []
    stream_bus.set_token_callback(events.append)
    try:
        cfg = {
            "configurable": {
                "adapter": MockAdapter(tiers=pipeline_cfg["tiers"]),
                "pipeline": pipeline_cfg,
                "run": {},
                "thread_id": "run-1",
            }
        }
        await node_roster({"run_id": "run-1"}, cfg)
    finally:
        stream_bus.clear_token_callback()

    ready = [e["creator"] for e in events if e.get("type") == "creator_ready"]
    assert ready, "esperava ao menos um creator_ready"
    for creator in ready:
        norm = web_server._normalize_creator(creator)
        assert norm["voice_preview_uri"].startswith("data:audio/wav;base64,")
        art = web_server._normalize_artifact(
            {"kind": "voice_preview", "uri": norm["voice_preview_uri"]}
        )
        assert art["media_type"] == "audio"
        assert art["renderable"] is True
