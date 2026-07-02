"""Testes das funções de roteamento (conditional edges): LTX-only + QC gate/loop."""
import pytest

from orchestrator.graph.routing import route_after_qc, route_after_script, select_tier
from orchestrator.graph.state import QCResult, new_item

TIER_NAMES = ["ltx", "kling", "seedance"]


# --- tier routing (Step 4): retries permanecem em LTX ---

def test_select_tier_stays_on_ltx_for_all_attempts():
    assert select_tier(0, TIER_NAMES) == "ltx"
    assert select_tier(1, TIER_NAMES) == "ltx"
    assert select_tier(2, TIER_NAMES) == "ltx"


def test_select_tier_stays_on_ltx_above_attempt_budget():
    assert select_tier(5, TIER_NAMES) == "ltx"


def test_route_after_script_uses_current_attempt():
    item = new_item({"x": 1})
    assert route_after_script(item, TIER_NAMES) == "ltx"
    item.attempts = 1
    assert route_after_script(item, TIER_NAMES) == "ltx"


# --- QC gate (Step 7): aprovado segue, reprovado volta, esgotado descarta ---

def test_qc_gate_pass_goes_to_assembly():
    item = new_item({"x": 1})
    item.qc = QCResult(passed=True, score=0.9)
    assert route_after_qc(item, max_attempts=3, tier_names=TIER_NAMES) == "assembly"


def test_qc_gate_fail_within_budget_regenerates_on_ltx():
    item = new_item({"x": 1})
    item.qc = QCResult(passed=False, score=0.2, reasons=["hands"])
    item.attempts = 1  # já reprovou uma vez; próxima geração continua em LTX
    assert route_after_qc(item, max_attempts=3, tier_names=TIER_NAMES) == "ltx"


def test_qc_gate_exhausted_drops():
    item = new_item({"x": 1})
    item.qc = QCResult(passed=False, score=0.1, reasons=["eyes"])
    item.attempts = 3  # atingiu o teto
    assert route_after_qc(item, max_attempts=3, tier_names=TIER_NAMES) == "drop"


def test_qc_gate_requires_qc_present():
    item = new_item({"x": 1})  # qc ainda None
    with pytest.raises(ValueError):
        route_after_qc(item, max_attempts=3, tier_names=TIER_NAMES)
