"""TDD — creator_store.py (Seção C do plano).

Espelha test_feedback_store.py:
- record/load, acumulação, ordenação desc por _idx
- status approved/rejected
- image/voice/prompts presentes
- store inexistente → []
"""
from __future__ import annotations

import json
import pytest

from orchestrator.creator_store import record_creators, load_creators


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CREATOR_A = {"id": "creator-0", "image": "mock://img/0.png", "voice": "voice-0"}
CREATOR_B = {"id": "creator-1", "image": "mock://img/1.png", "voice": "voice-1"}


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_load_creators_missing_store_returns_empty(tmp_path):
    store = tmp_path / "creators.json"
    assert load_creators(str(store)) == []


def test_record_and_load_creators_basic(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(
        str(store), "run-001",
        [CREATOR_A, CREATOR_B],
        approved_ids=["creator-0"],
        creator_prompt="portrait",
        video_prompt="action",
        offer="serum X",
    )
    entries = load_creators(str(store))
    assert len(entries) == 2
    ids = {e["creator_id"] for e in entries}
    assert ids == {"creator-0", "creator-1"}


def test_approved_status_set_correctly(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(
        str(store), "run-001",
        [CREATOR_A, CREATOR_B],
        approved_ids=["creator-0"],
    )
    entries = {e["creator_id"]: e for e in load_creators(str(store))}
    assert entries["creator-0"]["status"] == "approved"
    assert entries["creator-1"]["status"] == "rejected"


def test_all_rejected_when_approved_ids_empty(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(str(store), "run-001", [CREATOR_A], approved_ids=[])
    entries = load_creators(str(store))
    assert entries[0]["status"] == "rejected"


def test_fields_image_voice_prompts_present(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(
        str(store), "run-001",
        [CREATOR_A],
        approved_ids=["creator-0"],
        creator_prompt="P1",
        video_prompt="P2",
        offer="serum",
    )
    e = load_creators(str(store))[0]
    assert e["image"] == CREATOR_A["image"]
    assert e["voice"] == CREATOR_A["voice"]
    assert e["creator_prompt"] == "P1"
    assert e["video_prompt"] == "P2"
    assert e["offer"] == "serum"
    assert e["run_id"] == "run-001"


def test_no_idx_in_returned_entries(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(str(store), "run-001", [CREATOR_A], approved_ids=[])
    entries = load_creators(str(store))
    for e in entries:
        assert "_idx" not in e


def test_accumulation_across_two_runs(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(str(store), "run-001", [CREATOR_A], approved_ids=["creator-0"])
    record_creators(str(store), "run-002", [CREATOR_B], approved_ids=[])
    entries = load_creators(str(store))
    assert len(entries) == 2
    run_ids = {e["run_id"] for e in entries}
    assert run_ids == {"run-001", "run-002"}


def test_ordering_most_recent_first(tmp_path):
    """Entradas mais recentes (maior _idx) devem vir primeiro."""
    store = tmp_path / "creators.json"
    record_creators(str(store), "run-001", [CREATOR_A], approved_ids=[])
    record_creators(str(store), "run-002", [CREATOR_B], approved_ids=[])
    entries = load_creators(str(store))
    # run-002 foi adicionado por último → deve aparecer primeiro
    assert entries[0]["run_id"] == "run-002"
    assert entries[1]["run_id"] == "run-001"


def test_deterministic_json_on_disk(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(str(store), "run-001", [CREATOR_A], approved_ids=["creator-0"])
    content = (tmp_path / "creators.json").read_text()
    parsed = json.loads(content)
    expected = json.dumps(parsed, indent=2, sort_keys=True)
    assert content == expected


def test_creates_parent_directories(tmp_path):
    store = tmp_path / "nested" / "deep" / "creators.json"
    record_creators(str(store), "run-001", [CREATOR_A], approved_ids=[])
    assert store.exists()


def test_prompts_default_to_none(tmp_path):
    store = tmp_path / "creators.json"
    record_creators(str(store), "run-001", [CREATOR_A], approved_ids=[])
    e = load_creators(str(store))[0]
    assert e["creator_prompt"] is None
    assert e["video_prompt"] is None
    assert e["offer"] is None


def test_record_creators_persists_normalized_creator_fields(tmp_path):
    store = tmp_path / "creators.json"
    creator = {
        "id": "creator-9",
        "image_uri": "https://cdn.example/face.png",
        "voice_ref": "voice-9",
        "voice_preview_uri": "https://cdn.example/voice.mp3",
        "angles": ["front", "profile"],
    }
    record_creators(str(store), "run-001", [creator], approved_ids=["creator-9"])
    e = load_creators(str(store))[0]
    assert e["image_uri"] == "https://cdn.example/face.png"
    assert e["voice_ref"] == "voice-9"
    assert e["voice_preview_uri"] == "https://cdn.example/voice.mp3"
    assert e["image"] == "https://cdn.example/face.png"
    assert e["voice"] == "voice-9"
    assert e["angles"] == ["front", "profile"]


def test_load_creators_old_store_without_normalized_fields_still_loads(tmp_path):
    store = tmp_path / "creators.json"
    store.write_text(
        json.dumps(
            {
                "run-001:creator-0": {
                    "_idx": 0,
                    "run_id": "run-001",
                    "creator_id": "creator-0",
                    "image": "mock://img/0.png",
                    "voice": "voice-0",
                    "status": "approved",
                }
            }
        ),
        encoding="utf-8",
    )
    e = load_creators(str(store))[0]
    assert e["image"] == "mock://img/0.png"
    assert e["voice"] == "voice-0"
    assert e["image_uri"] == "mock://img/0.png"
    assert e["voice_ref"] == "voice-0"
    assert e["voice_preview_uri"] is None
