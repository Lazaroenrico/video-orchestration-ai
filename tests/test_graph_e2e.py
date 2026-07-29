"""Teste end-to-end do runner: pipeline mock completa, fan-out, QC-loop, determinismo."""
import pytest

from orchestrator import runner
from orchestrator.graph.state import Artifact, Item

PROVIDERS = {"adapters": {"video": "mock"}}


def test_summary_breaks_out_voiceover_cost():
    item = Item(
        id="item-1",
        concept={"hook": "h"},
        clips=[
            Artifact(
                kind="clip",
                uri="mock://clip",
                meta={"tier": "pruna", "cost_usd": 0.16},
            )
        ],
        voiceover=Artifact(
            kind="voiceover",
            uri="mock://voice",
            meta={"cost_usd": 0.0042},
        ),
        assembled=Artifact(
            kind="video",
            uri="mock://final",
            meta={"provider": "ffmpeg", "cost_usd": 0.0},
        ),
        cost_usd=0.1642,
    )

    summary = runner.summarize({"results": [item]})

    assert summary["cost_by_stage"] == {
        "video": 0.16,
        "voiceover": 0.0042,
        "assembly": 0.0,
    }
    assert summary["total_cost_usd"] == pytest.approx(0.1642)


def test_clean_task_error_variants():
    assert runner._clean_task_error("") == "task falhou"
    assert runner._clean_task_error(None) == "task falhou"
    assert runner._clean_task_error("RuntimeError('boom')") == "boom"
    assert runner._clean_task_error("ValueError('')") == "task falhou"  # msg vazia → fallback
    # stack trace do bridge Node (\n literais) é descartado
    cleaned = runner._clean_task_error("RuntimeError('falhou\\n    at fn (file.mjs:1:2)')")
    assert cleaned == "falhou"


async def test_get_pending_items_empty_when_no_state(tmp_path, pipeline_cfg, monkeypatch):
    class _App:
        async def aget_state(self, cfg, subgraphs=False):
            return None

    monkeypatch.setattr(runner, "build_graph", lambda *a, **k: _App())
    out = await runner.get_pending_items(
        pipeline_cfg, db_path=str(tmp_path / "x.db"), run_id="none"
    )
    assert out == []


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


async def test_run_pipeline_streams_item_aware_progress_events(tmp_path, pipeline_cfg):
    events = []

    async def collect(event):
        events.append(event)

    _, out = await runner.run_pipeline(
        pipeline_cfg,
        PROVIDERS,
        db_path=tmp_path / "progress.sqlite",
        batch=2,
        offer="serum",
        run_id="progress-e2e",
        event_sink=collect,
    )

    assert len(out["results"]) == 2
    assert any(
        event["stage_id"] == "concepts" and event["status"] == "completed"
        for event in events
    )
    assert [
        (event["completed_units"], event["total_units"])
        for event in events
        if event["stage_id"] == "scripts" and event["status"] == "progress"
    ] == [(1, 2), (2, 2)]
    assert [
        (event["completed_units"], event["total_units"])
        for event in events
        if event["stage_id"] == "creator_previews"
        and event["status"] == "progress"
    ] == [(1, 2), (2, 2)]
    item_events = [
        event
        for event in events
        if event["stage_id"] == "talking_head" and event.get("item_id")
    ]
    assert {event["item_id"] for event in item_events} == {
        item.id for item in runner.as_items(out["results"])
    }
    assert all(event["operation_id"] for event in item_events)


async def test_progress_stream_falls_back_to_root_output_without_snapshot():
    class _App:
        async def astream_events(self, input_value, config, version):
            assert input_value == {"run_id": "fallback"}
            assert config == {"configurable": {"thread_id": "fallback"}}
            assert version == "v2"
            yield {
                "event": "on_chain_end",
                "name": "LangGraph",
                "data": {"output": {"run_id": "fallback", "results": []}},
            }

        async def aget_state(self, config):
            return None

    events = []

    async def collect(event):
        events.append(event)

    output = await runner._invoke_with_progress(
        _App(),
        {"run_id": "fallback"},
        {"configurable": {"thread_id": "fallback"}},
        collect,
    )

    assert output == {"run_id": "fallback", "results": []}
    assert events == []


async def test_final_video_is_upscaled_not_the_image(tmp_path, pipeline_cfg):
    """O vídeo final montado passa pelo upscale; a imagem do creator fica crua."""
    db = tmp_path / "runs.sqlite"
    _, out = await runner.run_pipeline(
        pipeline_cfg, PROVIDERS, db_path=db, batch=5, offer="serum", run_id="upscale-e2e"
    )
    approved = [i for i in runner.as_items(out["results"]) if i.assembled is not None]
    assert approved, "esperado ao menos um item montado"
    for it in approved:
        assert it.assembled.meta.get("upscaled") is True          # vídeo final upscalado
        assert it.assembled.meta.get("upscaled_from")             # proveniência pré-upscale
    # A base do creator é a face crua (mock não upscala imagem) — sem meta 'upscaled'.
    for creator in out.get("roster") or []:
        assert "upscaled" not in str(creator.get("upscaled_base", ""))


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


async def test_get_pending_items_recovers_orphaned_item(tmp_path, pipeline_cfg, monkeypatch):
    """Um item que quebrou depois de gerar clips (crash na montagem, fora do try/except
    do node) não entra em `results`, mas deve ser recuperável do checkpoint com seus
    clips + o motivo do erro — é o que faz o run reaparecer na UI sem re-rodar."""
    from orchestrator.nodes import stages

    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path / "videos")
    orig = stages.media_store.persist_item_media

    async def crash_on_assembled(item, **kwargs):
        data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        if data.get("assembled"):  # só na montagem, depois de clips+qc já commitados
            raise RuntimeError(
                "Seedance bridge failed: input image may contain real person"
            )
        return await orig(item, **kwargs)

    monkeypatch.setattr(stages.media_store, "persist_item_media", crash_on_assembled)

    db = tmp_path / "runs.sqlite"
    with pytest.raises(Exception):
        await runner.run_pipeline(
            pipeline_cfg, PROVIDERS, db_path=db, batch=1, offer="serum", run_id="orphan-1"
        )

    pending = await runner.get_pending_items(pipeline_cfg, db_path=str(db), run_id="orphan-1")
    assert len(pending) == 1
    it = pending[0]
    assert len(it.clips) >= 1                    # clips gerados sobrevivem
    assert it.assembled is None                  # montagem não completou
    assert it.error and "real person" in it.error

    # E o canal `results` continua vazio — get_status sozinho não veria o item.
    state = await runner.get_status(pipeline_cfg, db_path=str(db), run_id="orphan-1")
    assert runner.as_items((state or {}).get("results")) == []
