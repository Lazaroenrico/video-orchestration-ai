"""Viés de geração de conceitos pelos hooks vencedores do ciclo anterior (Step 10->1)."""
import pytest

from orchestrator import runner
from orchestrator.adapters.mock import MockAdapter

TIERS = [
    {"name": "ltx", "model": "ltx-2.3", "cost_per_second": 0.01, "max_concurrency": 16},
    {"name": "kling", "model": "kling-3.0", "cost_per_second": 0.10, "max_concurrency": 6},
    {"name": "seedance", "model": "seedance-2.0", "cost_per_second": 0.168, "max_concurrency": 2},
]
PROVIDERS = {"adapters": {"video": "mock"}}


@pytest.fixture
def adapter():
    return MockAdapter(tiers=TIERS)


async def test_bias_none_is_backward_compatible(adapter):
    # Sem viés (default) o comportamento é idêntico ao anterior.
    a = await adapter.generate_concepts(offer="serum", n=10, seed="wk1")
    b = await adapter.generate_concepts(offer="serum", n=10, seed="wk1", bias=None)
    assert a == b


async def test_bias_is_deterministic(adapter):
    a = await adapter.generate_concepts(offer="serum", n=20, seed="wk1", bias=["problem"])
    b = await adapter.generate_concepts(offer="serum", n=20, seed="wk1", bias=["problem"])
    assert a == b


async def test_bias_increases_target_style_share(adapter):
    n = 60
    base = await adapter.generate_concepts(offer="serum", n=n, seed="wk1")
    biased = await adapter.generate_concepts(offer="serum", n=n, seed="wk1", bias=["problem"])
    base_share = sum(c["hook_style"] == "problem" for c in base)
    biased_share = sum(c["hook_style"] == "problem" for c in biased)
    assert biased_share > base_share
    assert biased_share >= n * 0.4          # viés relevante
    assert len({c["hook_style"] for c in biased}) > 1  # mantém spread


async def test_feedback_loop_biases_next_cycle(tmp_path, pipeline_cfg):
    # cycle-2 deve inclinar os conceitos para os winning_styles do cycle-1.
    store = tmp_path / "fb.json"
    db = tmp_path / "runs.sqlite"
    _, out1 = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=12, offer="serum",
        run_id="cycle-1", feedback_store=store,
    )
    winners = out1["feedback"]["winning_styles"]
    assert winners
    _, out2 = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=12, offer="serum",
        run_id="cycle-2", feedback_store=store,
    )
    top_winner = winners[0]
    share2 = sum(c["hook_style"] == top_winner for c in out2["concepts"])
    # com o viés, o hook vencedor aparece mais do que a fatia uniforme (~1/5)
    assert share2 > 12 / 5
