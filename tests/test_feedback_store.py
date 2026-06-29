"""Testes para feedback_store.py — TDD: escrito antes da implementação.

Cobre:
- round-trip save/load
- load_latest_feedback retorna o último por índice incremental
- store inexistente retorna None
- integração leve com node_feedback + tmp_path
"""
import json
import pytest

from orchestrator.feedback_store import save_feedback, load_feedback, load_latest_feedback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_SUMMARY = {
    "produced": 10,
    "approved": 8,
    "dropped": 2,
    "total_attempts": 3,
    "total_cost_usd": 1.23,
    "winning_styles": ["problem", "curiosity"],
}


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_save_and_load_feedback(tmp_path):
    store = tmp_path / "feedback.json"
    save_feedback(store, "run-001", SAMPLE_SUMMARY)

    result = load_feedback(store, "run-001")
    assert result is not None
    assert result["produced"] == 10
    assert result["approved"] == 8
    assert result["winning_styles"] == ["problem", "curiosity"]


def test_load_feedback_absent_run_returns_none(tmp_path):
    store = tmp_path / "feedback.json"
    save_feedback(store, "run-001", SAMPLE_SUMMARY)

    result = load_feedback(store, "run-999")
    assert result is None


def test_load_feedback_missing_store_returns_none(tmp_path):
    store = tmp_path / "nonexistent.json"
    assert load_feedback(store, "run-001") is None


def test_load_latest_feedback_missing_store_returns_none(tmp_path):
    store = tmp_path / "nonexistent.json"
    assert load_latest_feedback(store) is None


def test_load_latest_feedback_empty_store(tmp_path):
    # An empty JSON object is an edge case (store was created but no runs saved yet).
    # Should not happen through normal API but let's be safe.
    store = tmp_path / "feedback.json"
    store.write_text("{}")
    assert load_latest_feedback(store) is None


def test_multiple_runs_accumulate(tmp_path):
    store = tmp_path / "feedback.json"

    save_feedback(store, "run-001", {**SAMPLE_SUMMARY, "produced": 5})
    save_feedback(store, "run-002", {**SAMPLE_SUMMARY, "produced": 7})
    save_feedback(store, "run-003", {**SAMPLE_SUMMARY, "produced": 9})

    raw = json.loads(store.read_text())
    assert set(raw.keys()) == {"run-001", "run-002", "run-003"}


def test_load_latest_feedback_returns_last_saved(tmp_path):
    store = tmp_path / "feedback.json"

    save_feedback(store, "run-001", {**SAMPLE_SUMMARY, "produced": 1})
    save_feedback(store, "run-002", {**SAMPLE_SUMMARY, "produced": 2})
    save_feedback(store, "run-003", {**SAMPLE_SUMMARY, "produced": 3})

    latest = load_latest_feedback(store)
    assert latest is not None
    # run-003 was saved last (highest index)
    assert latest["produced"] == 3


def test_load_latest_feedback_single_entry(tmp_path):
    store = tmp_path / "feedback.json"
    save_feedback(store, "run-only", SAMPLE_SUMMARY)

    latest = load_latest_feedback(store)
    assert latest is not None
    assert latest["produced"] == SAMPLE_SUMMARY["produced"]


def test_overwrite_same_run_id(tmp_path):
    """Saving the same run_id twice should overwrite (last write wins), index updated."""
    store = tmp_path / "feedback.json"

    save_feedback(store, "run-001", {**SAMPLE_SUMMARY, "produced": 1})
    save_feedback(store, "run-002", {**SAMPLE_SUMMARY, "produced": 2})
    save_feedback(store, "run-001", {**SAMPLE_SUMMARY, "produced": 99})

    result = load_feedback(store, "run-001")
    assert result is not None
    assert result["produced"] == 99

    # After overwrite, run-001 has the highest index (saved last), so it's latest.
    latest = load_latest_feedback(store)
    assert latest is not None
    assert latest["produced"] == 99


def test_save_creates_parent_directories(tmp_path):
    store = tmp_path / "nested" / "deep" / "feedback.json"
    save_feedback(store, "run-001", SAMPLE_SUMMARY)
    assert store.exists()


def test_output_is_deterministic_json(tmp_path):
    store = tmp_path / "feedback.json"
    save_feedback(store, "run-001", SAMPLE_SUMMARY)

    content = store.read_text()
    parsed = json.loads(content)
    # Re-serialise with same settings; must match (sort_keys=True, indent=2)
    expected = json.dumps(parsed, indent=2, sort_keys=True)
    assert content == expected


# ---------------------------------------------------------------------------
# Integration test: node_feedback + feedback_store via config
# ---------------------------------------------------------------------------


async def test_node_feedback_writes_to_store(tmp_path):
    """node_feedback deve chamar save_feedback quando feedback_store está no config."""
    from orchestrator.graph.state import Item
    from orchestrator.nodes.stages import node_feedback

    store_path = tmp_path / "fb_store.json"

    # Build a minimal state with two items (one distributed, one dropped)
    item_ok = Item(
        concept={"hook_style": "curiosity"},
        distributed=True,
        cost_usd=0.5,
        attempts=1,
    )
    item_drop = Item(
        concept={"hook_style": "problem"},
        dropped=True,
        cost_usd=0.2,
        attempts=2,
    )

    state = {
        "run_id": "integration-run-1",
        "results": [item_ok, item_drop],
    }
    config = {
        "configurable": {
            "feedback_store": str(store_path),
        }
    }

    result = await node_feedback(state, config)

    # The return value must still be {"feedback": summary} — backward compat.
    assert "feedback" in result
    fb = result["feedback"]
    assert fb["produced"] == 2
    assert fb["approved"] == 1
    assert fb["dropped"] == 1

    # Side-effect: file was written.
    assert store_path.exists(), "feedback_store file should have been created"

    loaded = load_latest_feedback(store_path)
    assert loaded is not None
    assert loaded["produced"] == 2
    assert loaded["approved"] == 1


async def test_node_feedback_no_store_in_config(tmp_path):
    """Se feedback_store não está no config, node_feedback funciona normalmente sem erros."""
    from orchestrator.graph.state import Item
    from orchestrator.nodes.stages import node_feedback

    item = Item(
        concept={"hook_style": "curiosity"},
        distributed=True,
        cost_usd=0.5,
    )

    state = {"run_id": "run-no-store", "results": [item]}
    # No feedback_store key in configurable
    config = {"configurable": {}}

    result = await node_feedback(state, config)
    assert "feedback" in result
    assert result["feedback"]["produced"] == 1
