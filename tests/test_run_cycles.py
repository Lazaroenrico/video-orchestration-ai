"""N ciclos encadeados: cada ciclo lê o feedback do anterior e salva o seu (item 5 do v2).

Exercita a arquitetura de close-the-loop já existente (Step 10 -> Step 1), agora
em cadeia: o vencedor do ciclo i vira viés do ciclo i+1.
"""
import pytest

from orchestrator import runner
from orchestrator.feedback_store import load_latest_feedback

PROVIDERS = {"adapters": {"video": "mock"}}


async def test_run_cycles_chains_feedback(tmp_path, pipeline_cfg):
    store = tmp_path / "feedback.json"
    results = await runner.run_cycles(
        pipeline_cfg, PROVIDERS, db_path=tmp_path / "runs.sqlite",
        cycles=3, feedback_store=store, batch=6, offer="serum",
        run_id_prefix="chain",
    )

    # um (run_id, out) por ciclo, com thread_ids distintos (checkpoints separados)
    assert len(results) == 3
    rids = [rid for rid, _ in results]
    assert len(set(rids)) == 3
    assert all(rid.startswith("chain") for rid in rids)

    # o primeiro ciclo não tem viés anterior; os seguintes herdam o vencedor do anterior
    assert results[0][1]["config"]["prior_winning_styles"] == []
    for i in range(1, 3):
        prior = results[i][1]["config"]["prior_winning_styles"]
        prev_winners = results[i - 1][1]["feedback"]["winning_styles"]
        assert prior == prev_winners

    # o store termina apontando para o feedback do último ciclo
    fb = load_latest_feedback(store)
    assert fb["produced"] == 6


async def test_run_cycles_requires_feedback_store(tmp_path, pipeline_cfg):
    with pytest.raises(ValueError):
        await runner.run_cycles(
            pipeline_cfg, PROVIDERS, db_path=tmp_path / "runs.sqlite",
            cycles=2, feedback_store=None,
        )


async def test_run_cycles_rejects_non_positive(tmp_path, pipeline_cfg):
    with pytest.raises(ValueError):
        await runner.run_cycles(
            pipeline_cfg, PROVIDERS, db_path=tmp_path / "runs.sqlite",
            cycles=0, feedback_store=tmp_path / "feedback.json",
        )
