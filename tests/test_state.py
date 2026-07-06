"""Testes dos schemas de estado do grafo (TDD — escritos antes da implementação)."""
import operator

import pytest

from orchestrator.graph.state import (
    Artifact,
    BatchState,
    Item,
    JudgeVerdict,
    QCResult,
    new_item,
)


def test_new_item_defaults():
    item = new_item(concept={"hook": "h", "angle": "a"}, creator_ref="creator-1")
    assert item.concept == {"hook": "h", "angle": "a"}
    assert item.creator_ref == "creator-1"
    assert item.attempts == 0
    assert item.clips == []
    assert item.script is None
    assert item.qc is None
    assert item.assembled is None
    assert item.dropped is False
    assert item.cost_usd == 0.0
    # id é estável/único e não vazio
    assert isinstance(item.id, str) and item.id


def test_new_item_unique_ids():
    a = new_item(concept={"x": 1})
    b = new_item(concept={"x": 1})
    assert a.id != b.id


def test_artifact_roundtrip():
    art = Artifact(kind="clip", uri="mock://clip/1", meta={"tier": "ltx", "seconds": 8})
    assert art.kind == "clip"
    assert art.meta["tier"] == "ltx"


def test_qcresult_fields():
    qc = QCResult(passed=False, score=0.4, reasons=["hands", "eyes"])
    assert qc.passed is False
    assert qc.score == 0.4
    assert "hands" in qc.reasons


def test_judge_verdict_from_response_derives_passed_above_threshold():
    v = JudgeVerdict.from_response(score=0.91, verdict=None, threshold=0.8)
    assert v.score == 0.91
    assert v.passed is True
    assert v.verdict == "pass"


def test_judge_verdict_from_response_below_threshold():
    v = JudgeVerdict.from_response(score=0.5, verdict=None, threshold=0.8)
    assert v.passed is False
    assert v.verdict == "fail"


def test_judge_verdict_explicit_verdict_overrides_threshold():
    # Se o gateway devolve verdict explícito, ele manda (mesmo contra o threshold).
    v = JudgeVerdict.from_response(score=0.5, verdict="pass", threshold=0.8)
    assert v.passed is True
    assert v.verdict == "pass"


def test_batchstate_has_expected_keys_and_reducers():
    # BatchState é um TypedDict com reducers nas chaves acumuladas em paralelo.
    ann = BatchState.__annotations__
    for key in ("run_id", "concepts", "roster", "results", "total_cost_usd", "config"):
        assert key in ann, f"falta a chave {key} em BatchState"


def test_results_reducer_is_additive():
    # O fan-out paralelo concatena resultados; o reducer precisa ser aditivo.
    from orchestrator.graph.state import add_items, add_cost

    merged = add_items([new_item({"a": 1})], [new_item({"b": 2})])
    assert len(merged) == 2
    assert add_cost(7.5, 8.0) == pytest.approx(15.5)


def test_item_is_pydantic_serializable():
    item = new_item(concept={"hook": "h"})
    item.clips.append(Artifact(kind="clip", uri="mock://c"))
    dumped = item.model_dump()
    assert dumped["clips"][0]["uri"] == "mock://c"
    # round-trip
    again = Item.model_validate(dumped)
    assert again.clips[0].uri == "mock://c"
