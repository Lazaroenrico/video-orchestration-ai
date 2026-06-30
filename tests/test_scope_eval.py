"""LLM Judge — aderência ao escopo: offer + system_prompts (Seção G do plano).

CI:    pytest tests/test_scope_eval.py          -> replay via cassette, sem rede.
Live:  pytest tests/test_scope_eval.py --live   -> chama gateway real (JUDGE_GATEWAY_URL).
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from orchestrator.adapters.judge import (
    Cassette,
    CassetteMiss,
    GatewayJudge,
    evaluate_judge,
    scope_adherence_evaluator,
    SCOPE_CRITERIA,
)
from orchestrator.config import load_judge

CASSETTE = Path(__file__).parent / "cassettes" / "scope_eval.json"

# Dataset de escopo: subjects incluem offer + prompts + output
# 3 dentro do escopo (expected_pass: True) + 2 fora (expected_pass: False)
DATASET = [
    {
        "id": "scope-1",
        "subject": {
            "id": "scope-1",
            "offer": "serum anti-aging X",
            "creator_prompt": "influencer jovem, estilo casual",
            "video_prompt": "demonstra o produto no espelho, começa com problema de pele",
            "output": "Uso esse serum todo dia e minha pele melhorou muito. Começa com antes/depois.",
        },
        "expected_pass": True,
    },
    {
        "id": "scope-2",
        "subject": {
            "id": "scope-2",
            "offer": "serum anti-aging X",
            "creator_prompt": "especialista em skincare, tom científico",
            "video_prompt": "explica ingrediente ativo, foca no retinol",
            "output": "O retinol do serum estimula colágeno. Resultados em 4 semanas.",
        },
        "expected_pass": True,
    },
    {
        "id": "scope-3",
        "subject": {
            "id": "scope-3",
            "offer": "serum anti-aging X",
            "creator_prompt": "lifestyle creator, humor leve",
            "video_prompt": "antes e depois rápido, CTA urgente",
            "output": "Minha pele nunca esteve tão boa! Link na bio, oferta só hoje.",
        },
        "expected_pass": True,
    },
    {
        "id": "scope-4",
        "subject": {
            "id": "scope-4",
            "offer": "serum anti-aging X",
            "creator_prompt": "influencer jovem, estilo casual",
            "video_prompt": "demonstra o produto no espelho",
            "output": "Esse tênis é incrível! Comprei semana passada e já usei 5 vezes.",
        },
        "expected_pass": False,
    },
    {
        "id": "scope-5",
        "subject": {
            "id": "scope-5",
            "offer": "serum anti-aging X",
            "creator_prompt": "tom sério, sem humor",
            "video_prompt": "foco em benefícios clínicos",
            "output": "hahaha que engraçado esse produto! minha gata topou meu skincare hoje kkkk",
        },
        "expected_pass": False,
    },
]


@pytest.fixture
def judge_config():
    return load_judge("config")


# ---------------------------------------------------------------------------
# unit: build_request inclui placeholders do subject de escopo
# ---------------------------------------------------------------------------


def test_build_request_includes_scope_subject(judge_config):
    j = GatewayJudge(judge_config)
    req = j.build_request(SCOPE_CRITERIA, DATASET[0]["subject"])
    assert "offer" in req["content"] or "scope-1" in req["content"]
    assert "on_offer" in req["content"]


# ---------------------------------------------------------------------------
# unit: SCOPE_CRITERIA e scope_adherence_evaluator
# ---------------------------------------------------------------------------


def test_scope_criteria_has_required_keys():
    assert "on_offer" in SCOPE_CRITERIA
    assert "on_prompt" in SCOPE_CRITERIA
    assert "no_offtopic" in SCOPE_CRITERIA


def test_scope_adherence_evaluator_correct():
    from orchestrator.graph.state import JudgeVerdict
    v_pass = JudgeVerdict(score=0.9, verdict="pass", passed=True)
    ev = scope_adherence_evaluator(v_pass, True)
    assert ev["key"] == "scope_adherence"
    assert ev["score"] == 1.0


def test_scope_adherence_evaluator_incorrect():
    from orchestrator.graph.state import JudgeVerdict
    v_fail = JudgeVerdict(score=0.3, verdict="fail", passed=False)
    ev = scope_adherence_evaluator(v_fail, True)  # esperava pass, veio fail
    assert ev["score"] == 0.0


# ---------------------------------------------------------------------------
# CassetteMiss explícito
# ---------------------------------------------------------------------------


def test_scope_cassette_miss_is_explicit(judge_config):
    j = GatewayJudge(judge_config, cassette=Cassette(CASSETTE), live=False)
    with pytest.raises(CassetteMiss):
        j.judge(SCOPE_CRITERIA, {"id": "nao-existe"}, key="nao-existe")


# ---------------------------------------------------------------------------
# replay determinístico (CI)
# ---------------------------------------------------------------------------


def test_scope_replay_is_deterministic(judge_config):
    j = GatewayJudge(judge_config, cassette=Cassette(CASSETTE), live=False)
    v1 = j.judge(SCOPE_CRITERIA, DATASET[0]["subject"], key="scope-1")
    v2 = j.judge(SCOPE_CRITERIA, DATASET[0]["subject"], key="scope-1")
    assert v1 == v2
    assert v1.score == 0.94
    assert v1.passed is True


def test_scope_evaluate_judge_accuracy_is_1(judge_config):
    j = GatewayJudge(judge_config, cassette=Cassette(CASSETTE), live=False)
    report = evaluate_judge(j, DATASET, criteria=SCOPE_CRITERIA, evaluator=scope_adherence_evaluator)
    assert report["n"] == 5
    assert report["accuracy"] == 1.0
    # idempotência
    assert evaluate_judge(j, DATASET, criteria=SCOPE_CRITERIA, evaluator=scope_adherence_evaluator) == report


# ---------------------------------------------------------------------------
# evaluate_judge: retrocompatível (sem criteria/evaluator explícitos)
# ---------------------------------------------------------------------------


def test_evaluate_judge_backwards_compatible(judge_config):
    """Sem criteria/evaluator, deve usar DEFAULT_QC_CRITERIA + qc_correctness_evaluator."""
    from orchestrator.adapters.judge import DEFAULT_QC_CRITERIA, qc_correctness_evaluator
    from orchestrator.config import load_judge
    from orchestrator.adapters.judge import Cassette, GatewayJudge, evaluate_judge
    from pathlib import Path

    QC_CASSETTE = Path(__file__).parent / "cassettes" / "judge_qc.json"
    QC_DATASET = [
        {"id": "clip-1", "subject": {"id": "clip-1", "uri": "mock://clip/1"}, "expected_pass": True},
        {"id": "clip-2", "subject": {"id": "clip-2", "uri": "mock://clip/2"}, "expected_pass": True},
        {"id": "clip-3", "subject": {"id": "clip-3", "uri": "mock://clip/3"}, "expected_pass": False},
        {"id": "clip-4", "subject": {"id": "clip-4", "uri": "mock://clip/4"}, "expected_pass": False},
        {"id": "clip-5", "subject": {"id": "clip-5", "uri": "mock://clip/5"}, "expected_pass": True},
    ]
    j = GatewayJudge(load_judge("config"), cassette=Cassette(QC_CASSETTE), live=False)
    report = evaluate_judge(j, QC_DATASET)  # sem criteria/evaluator
    assert report["accuracy"] == 1.0


# ---------------------------------------------------------------------------
# live com gateway fake (sempre roda, exercita o caminho HTTP)
# ---------------------------------------------------------------------------


def test_live_scope_via_fake_gateway(judge_config, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"score": 0.92, "verdict": "pass"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cas = Cassette(tmp_path / "scope_rec.json")
    j = GatewayJudge(judge_config, cassette=cas, live=True, client=client)
    v = j.judge(SCOPE_CRITERIA, {"id": "scope-x"}, key="scope-x")
    assert v.passed is True
    assert v.score == 0.92
    # replay
    replay = GatewayJudge(judge_config, cassette=Cassette(tmp_path / "scope_rec.json"), live=False)
    assert replay.judge(SCOPE_CRITERIA, {"id": "scope-x"}, key="scope-x").score == 0.92


# ---------------------------------------------------------------------------
# live opt-in (pula sem JUDGE_GATEWAY_URL)
# ---------------------------------------------------------------------------


def test_scope_live_records_cassette(judge_config, live, tmp_path):
    if not live:
        pytest.skip("teste live: rode com --live")
    if not os.environ.get("JUDGE_GATEWAY_URL"):
        pytest.skip("--live requer JUDGE_GATEWAY_URL")

    import shutil
    tmp_cassette = tmp_path / "scope_eval.json"
    cas = Cassette(tmp_cassette)
    j = GatewayJudge(load_judge("config"), cassette=cas, live=True)
    for ex in DATASET:
        v = j.judge(SCOPE_CRITERIA, ex["subject"], key=ex["id"])
        assert 0.0 <= v.score <= 1.0
    shutil.copy(tmp_cassette, CASSETTE)
