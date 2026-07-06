"""Testes de persistência/resumibilidade via checkpointer (AsyncSqliteSaver)."""
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer


async def test_open_checkpointer_creates_file(tmp_path):
    db = tmp_path / "runs" / "state.sqlite"
    async with open_checkpointer(db) as cp:
        assert cp is not None
    assert db.exists()


async def test_run_state_is_persisted_and_readable(tmp_path, run_config):
    db = tmp_path / "state.sqlite"
    pipeline = run_config["configurable"]["pipeline"]
    thread = {
        "configurable": dict(run_config["configurable"], thread_id="run-xyz"),
        "max_concurrency": 4,
        "recursion_limit": 50,
    }
    init = {"run_id": "run-xyz", "config": {"offer": "serum", "batch_size": 4}}

    async with open_checkpointer(db) as cp:
        app = build_graph(pipeline, checkpointer=cp)
        out = await app.ainvoke(init, thread)
        assert len(out["results"]) == 4
        # estado lido de volta do checkpoint pelo mesmo thread_id
        snap = await app.aget_state(thread)
        assert snap.values["run_id"] == "run-xyz"
        assert len(snap.values["results"]) == 4


async def test_alist_yields_checkpoints_for_thread(tmp_path, run_config):
    """A fachada async ``alist`` itera os checkpoints gravados de um thread."""
    db = tmp_path / "state.sqlite"
    pipeline = run_config["configurable"]["pipeline"]
    thread = {
        "configurable": dict(run_config["configurable"], thread_id="run-list"),
        "max_concurrency": 4,
        "recursion_limit": 50,
    }

    async with open_checkpointer(db) as cp:
        app = build_graph(pipeline, checkpointer=cp)
        await app.ainvoke({"run_id": "run-list", "config": {"batch_size": 2}}, thread)

        rows = [row async for row in cp.alist({"configurable": {"thread_id": "run-list"}})]

    assert len(rows) >= 1


async def test_resume_with_fresh_app_instance_reads_checkpoint(tmp_path, run_config):
    # "Continuar de onde parou": nova instância do app, mesmo arquivo/thread.
    db = tmp_path / "state.sqlite"
    pipeline = run_config["configurable"]["pipeline"]
    base_cfg = dict(run_config["configurable"], thread_id="run-1")

    async with open_checkpointer(db) as cp1:
        app1 = build_graph(pipeline, checkpointer=cp1)
        await app1.ainvoke(
            {"run_id": "run-1", "config": {"batch_size": 3}},
            {"configurable": base_cfg, "max_concurrency": 4, "recursion_limit": 50},
        )

    # nova instância (novo processo simulado) abrindo o mesmo arquivo
    async with open_checkpointer(db) as cp2:
        app2 = build_graph(pipeline, checkpointer=cp2)
        snap = await app2.aget_state({"configurable": base_cfg})
        assert snap.values["run_id"] == "run-1"
        assert len(snap.values["results"]) == 3
