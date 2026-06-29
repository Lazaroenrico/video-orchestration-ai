"""Teste end-to-end do runner: pipeline mock completa, fan-out, QC-loop, determinismo."""
import pytest

from orchestrator import runner

PROVIDERS = {"adapters": {"video": "mock"}}


async def test_run_pipeline_end_to_end(tmp_path, pipeline_cfg):
    db = tmp_path / "runs.sqlite"
    rid, out = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=10, offer="serum", run_id="e2e-1"
    )
    s = runner.summarize({**out, "run_id": rid})
    assert s["produced"] == 10
    assert s["approved"] + s["dropped"] == 10
    assert s["in_flight"] == 0
    assert s["total_cost_usd"] > 0
    assert "ltx" in s["cost_by_tier"]  # bulk barato sempre presente


async def test_qc_loop_runs_within_attempt_budget(tmp_path, pipeline_cfg):
    db = tmp_path / "runs.sqlite"
    _, out = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=20, offer="serum", run_id="e2e-budget"
    )
    items = runner.as_items(out["results"])
    assert any(i.attempts >= 1 for i in items)             # loop exercitado
    assert all(i.attempts <= pipeline_cfg["qc"]["max_attempts"] for i in items)


async def test_run_is_deterministic(tmp_path, pipeline_cfg):
    # Mesmo run_id/oferta -> mesmo resultado agregado (dbs separados).
    _, a = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=tmp_path / "a.sqlite", batch=12, offer="serum", run_id="det"
    )
    _, b = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=tmp_path / "b.sqlite", batch=12, offer="serum", run_id="det"
    )
    sa = runner.summarize({**a, "run_id": "det"})
    sb = runner.summarize({**b, "run_id": "det"})
    assert sa == sb


async def test_resume_after_completion_is_consistent(tmp_path, pipeline_cfg):
    db = tmp_path / "runs.sqlite"
    rid, out = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=8, offer="serum", run_id="res-1"
    )
    produced = len(out["results"])
    # status lido do checkpoint bate com o run
    state = await runner.get_status(pipeline_cfg, db_path=db, run_id=rid)
    assert state is not None
    assert len(runner.as_items(state["results"])) == produced
