"""Contratos do dashboard human-on-the-loop via SSE."""
from __future__ import annotations

from pathlib import Path

from orchestrator.graph.state import Artifact, QCResult
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
