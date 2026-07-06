"""LLM Judge: avaliação determinística via cassette (CI) + modo --live opcional.

CI:    pytest tests/test_judge_eval.py           -> replay do cassette, sem rede.
Live:  pytest tests/test_judge_eval.py --live     -> chama o gateway real (JUDGE_GATEWAY_URL)
       e regrava o cassette. Requer a env; sem ela, o teste live é pulado.
"""
import os
import shutil
from pathlib import Path

import httpx
import pytest

from orchestrator.adapters.judge import (
    Cassette,
    CassetteMiss,
    DEFAULT_QC_CRITERIA,
    GatewayJudge,
    dig,
    evaluate_judge,
)
from orchestrator.config import load_judge

CASSETTE = Path(__file__).parent / "cassettes" / "judge_qc.json"

# Dataset de QC com rótulos "humanos" (ground truth). O judge deve concordar com eles.
DATASET = [
    {"id": "clip-1", "subject": {"id": "clip-1", "uri": "mock://clip/1"}, "expected_pass": True},
    {"id": "clip-2", "subject": {"id": "clip-2", "uri": "mock://clip/2"}, "expected_pass": True},
    {"id": "clip-3", "subject": {"id": "clip-3", "uri": "mock://clip/3"}, "expected_pass": False},
    {"id": "clip-4", "subject": {"id": "clip-4", "uri": "mock://clip/4"}, "expected_pass": False},
    {"id": "clip-5", "subject": {"id": "clip-5", "uri": "mock://clip/5"}, "expected_pass": True},
]


@pytest.fixture
def judge_config():
    return load_judge("config")


# ---------------- unit: request config-driven + extração ----------------


def test_build_request_substitutes_template(judge_config):
    j = GatewayJudge(judge_config)
    req = j.build_request(DEFAULT_QC_CRITERIA, {"id": "clip-1", "uri": "mock://c"})
    assert req["method"] == "POST"
    assert "clip-1" in req["content"]
    assert "real_test" in req["content"]  # criteria embutido


def test_dig_dotted_path():
    assert dig({"a": {"b": {"c": 7}}}, "a.b.c") == 7


# ---------------- replay determinístico (CI) ----------------


def test_judge_replay_matches_golden(judge_config):
    j = GatewayJudge(judge_config, cassette=Cassette(CASSETTE), live=False)
    v1 = j.judge(DEFAULT_QC_CRITERIA, DATASET[0]["subject"], key="clip-1")
    v2 = j.judge(DEFAULT_QC_CRITERIA, DATASET[0]["subject"], key="clip-1")
    assert v1.score == 0.93 and v1.passed is True
    assert v1 == v2  # determinístico


def test_judge_cassette_miss_is_explicit(judge_config):
    j = GatewayJudge(judge_config, cassette=Cassette(CASSETTE), live=False)
    with pytest.raises(CassetteMiss):
        j.judge(DEFAULT_QC_CRITERIA, {"id": "nao-existe"}, key="nao-existe")


def test_judge_requires_key_or_subject_id(judge_config):
    j = GatewayJudge(judge_config, cassette=Cassette(CASSETTE), live=False)
    with pytest.raises(ValueError, match="precisa de 'id'"):
        j.judge(DEFAULT_QC_CRITERIA, {})  # sem id e sem key


def test_judge_verdict_none_when_verdict_path_missing(judge_config, tmp_path):
    """Resposta sem o campo de verdict não quebra — cai no threshold sobre o score."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"score": 0.9}})  # sem verdict

    client = httpx.Client(transport=httpx.MockTransport(handler))
    j = GatewayJudge(judge_config, cassette=Cassette(tmp_path / "r.json"), live=True, client=client)

    v = j.judge(DEFAULT_QC_CRITERIA, {"id": "clip-z"}, key="clip-z")

    assert v.score == 0.9
    assert v.passed is True  # verdict ausente → decide pelo threshold


def test_judge_live_path_uses_own_client(judge_config, tmp_path, monkeypatch):
    """Sem client injetado, o gateway cria (e fecha) o próprio httpx.Client."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"score": 0.77, "verdict": "pass"}})

    import orchestrator.adapters.judge as judge_mod

    real_client = httpx.Client  # captura antes de patchar (módulo httpx é global)
    monkeypatch.setattr(
        judge_mod.httpx, "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )
    j = GatewayJudge(judge_config, cassette=Cassette(tmp_path / "r.json"), live=True)  # sem client

    v = j.judge(DEFAULT_QC_CRITERIA, {"id": "clip-o"}, key="clip-o")

    assert v.score == 0.77


def test_evaluate_judge_accuracy_is_deterministic(judge_config):
    j = GatewayJudge(judge_config, cassette=Cassette(CASSETTE), live=False)
    report = evaluate_judge(j, DATASET)
    assert report["n"] == 5
    assert report["accuracy"] == 1.0  # judge concorda com todos os rótulos do golden
    # rodar de novo dá exatamente o mesmo
    assert evaluate_judge(j, DATASET) == report


# ---------------- live (opt-in) ----------------


def test_judge_live_records_cassette(judge_config, live, tmp_path):
    if not live:
        pytest.skip("teste live: rode com --live")
    if not os.environ.get("JUDGE_GATEWAY_URL"):
        pytest.skip("--live requer JUDGE_GATEWAY_URL apontando p/ o gateway real")

    # grava num cassette temporário (não sobrescreve o golden até validado)
    tmp_cassette = tmp_path / "judge_qc.json"
    cas = Cassette(tmp_cassette)
    cfg = load_judge("config")  # já com env expandida (url/key reais)
    j = GatewayJudge(cfg, cassette=cas, live=True)
    for ex in DATASET:
        v = j.judge(DEFAULT_QC_CRITERIA, ex["subject"], key=ex["id"])
        assert 0.0 <= v.score <= 1.0
    assert tmp_cassette.exists()
    assert set(cas.data.keys()) == {ex["id"] for ex in DATASET}
    # promove o cassette gravado para o golden (regrava)
    shutil.copy(tmp_cassette, CASSETTE)


# ---------------- live com gateway fake (sempre roda, exercita o caminho HTTP) ----------------


def test_live_path_records_via_fake_gateway(judge_config, tmp_path):
    """Exercita o caminho live/record sem infra externa, usando um transport fake."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"score": 0.88, "verdict": "pass"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cas = Cassette(tmp_path / "rec.json")
    j = GatewayJudge(judge_config, cassette=cas, live=True, client=client)
    v = j.judge(DEFAULT_QC_CRITERIA, {"id": "clip-x"}, key="clip-x")
    assert v.passed is True and v.score == 0.88
    # gravou no cassette e o replay devolve o mesmo sem rede
    replay = GatewayJudge(judge_config, cassette=Cassette(tmp_path / "rec.json"), live=False)
    assert replay.judge(DEFAULT_QC_CRITERIA, {"id": "clip-x"}, key="clip-x").score == 0.88
