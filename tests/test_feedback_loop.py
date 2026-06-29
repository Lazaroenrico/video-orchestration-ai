"""Loop de feedback ponta a ponta: run persiste o agregado e o ciclo seguinte o lê."""
from orchestrator import runner
from orchestrator.feedback_store import load_latest_feedback

PROVIDERS = {"adapters": {"video": "mock"}}


async def test_run_pipeline_persists_feedback(tmp_path, pipeline_cfg):
    store = tmp_path / "feedback.json"
    rid, _ = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=tmp_path / "runs.sqlite",
        batch=8, offer="serum", run_id="loop-1", feedback_store=store,
    )
    fb = load_latest_feedback(store)
    assert fb is not None
    assert fb["produced"] == 8
    assert fb["approved"] + fb["dropped"] == 8
    assert "winning_styles" in fb


async def test_second_run_can_read_prior_feedback(tmp_path, pipeline_cfg):
    store = tmp_path / "feedback.json"
    db = tmp_path / "runs.sqlite"
    await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=6, offer="serum",
        run_id="cycle-1", feedback_store=store,
    )
    # ciclo seguinte: o runner carrega o feedback anterior e o expõe no resultado
    _, out = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=6, offer="serum",
        run_id="cycle-2", feedback_store=store,
    )
    assert out["config"].get("prior_winning_styles") is not None
    # o store agora tem os dois ciclos; o mais recente é o cycle-2
    fb = load_latest_feedback(store)
    assert fb["produced"] == 6
