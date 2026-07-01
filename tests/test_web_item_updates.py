"""Contratos do dashboard human-on-the-loop via SSE."""
from __future__ import annotations

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
                        "distributed": True,
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
    assert item["distributed"] is True
    assert item["dropped"] is False
    assert item["cost_usd"] == 0.11
    assert {a["uri"] for a in item["artifacts"]} == {
        "mock://clip/item-1/demo",
        "https://cdn.example/final.mp4",
    }


def test_ui_handles_item_update_and_keeps_item_text_dom_safe() -> None:
    html = Path("src/orchestrator/web/static/index.html").read_text(encoding="utf-8")
    assert 'case "item_update"' in html
    assert "function renderItem" in html
    render_body = html.split("function renderItem(item)", 1)[1].split("\nfunction ", 1)[0]
    assert ".textContent" in render_body
    assert "innerHTML = `" not in render_body


def test_ui_stream_panel_logs_non_llm_events() -> None:
    html = Path("src/orchestrator/web/static/index.html").read_text(encoding="utf-8")
    assert "function appendStreamLine" in html
    assert 'case "creator_start"' in html
    assert 'appendStreamLine("creator", `gerando ${ev.creator_id}`)' in html
    assert 'appendStreamLine("run", "pipeline iniciada")' in html


def test_creators_history_exposes_store_path_and_entries(tmp_path, monkeypatch) -> None:
    store = tmp_path / "creators.json"
    creator_store = web_server.creator_store
    creator_store.record_creators(
        store,
        "run-1",
        [{"id": "creator-0", "image": "/media/run-1/creator-0/image.png", "voice": "voice-0"}],
        approved_ids=["creator-0"],
    )
    monkeypatch.setenv("ORCH_CREATORS", str(store))

    import asyncio

    payload = asyncio.run(web_server.creators_history())

    assert payload["store_path"] == str(store)
    assert payload["exists"] is True
    assert payload["creators"][0]["creator_id"] == "creator-0"


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
