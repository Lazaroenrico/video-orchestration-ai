"""Combined creative-plan review gate."""
from __future__ import annotations

import pytest
from langgraph.types import Command

from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from tests.conftest import TIERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(adapter, pipeline_cfg, run_extras=None, thread_id="t1", db_path=None):
    run = {"platform": "tiktok", **(run_extras or {})}
    cfg: dict = {
        "configurable": {
            "adapter": adapter,
            "pipeline": pipeline_cfg,
            "run": run,
            "thread_id": thread_id,
        },
        "recursion_limit": 100,
    }
    return cfg


# ---------------------------------------------------------------------------
# Gate OFF — passthrough, testes existentes não quebram
# ---------------------------------------------------------------------------


async def test_approval_gate_off_runs_end_to_end(tmp_path, adapter, pipeline_cfg):
    """Sem approve_creators a pipeline roda fim a fim (passthrough)."""
    db = tmp_path / "runs.sqlite"
    cfg = _make_cfg(adapter, pipeline_cfg, thread_id="off-1")
    init = {"run_id": "off-1", "config": {"offer": "serum", "batch_size": 4}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        result = await graph.ainvoke(init, cfg)

    roster = result.get("roster", [])
    assert len(roster) == pipeline_cfg["roster"]["creators"]
    # todos os conceitos foram processados
    assert len(result.get("results", [])) == 4


async def test_approval_gate_off_roster_intact(tmp_path, adapter, pipeline_cfg):
    """gate off: roster não é filtrado."""
    db = tmp_path / "runs.sqlite"
    cfg = _make_cfg(adapter, pipeline_cfg, thread_id="off-2")
    init = {"run_id": "off-2", "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        result = await graph.ainvoke(init, cfg)

    assert len(result["roster"]) == pipeline_cfg["roster"]["creators"]


# ---------------------------------------------------------------------------
# Combined review ON — interrupt + resume
# ---------------------------------------------------------------------------


async def test_review_gate_on_pauses_at_interrupt(tmp_path, adapter, pipeline_cfg):
    db = tmp_path / "runs.sqlite"
    thread_id = "on-1"
    cfg = _make_cfg(
        adapter,
        pipeline_cfg,
        run_extras={"review_plan": True},
        thread_id=thread_id,
    )
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        await graph.ainvoke(init, cfg)
        snap = await graph.aget_state(cfg)

    assert snap.next
    all_interrupts = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
    assert len(all_interrupts) == 1
    payload = all_interrupts[0].value
    assert payload["type"] == "review_creative_plan"
    assert len(payload["concepts"]) == 2
    assert len(payload["creators"]) == pipeline_cfg["roster"]["creators"]


async def test_review_gate_approve_resumes_full_production(tmp_path, adapter, pipeline_cfg):
    db = tmp_path / "runs.sqlite"
    thread_id = "on-2"
    cfg = _make_cfg(
        adapter,
        pipeline_cfg,
        run_extras={"review_plan": True},
        thread_id=thread_id,
    )
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        await graph.ainvoke(init, cfg)
        result = await graph.ainvoke(Command(resume={"action": "approve"}), cfg)

    roster = result.get("roster", [])
    assert len(roster) == 2
    assert len(result.get("results", [])) == 2
    assert result["review_approved"] is True


async def test_review_gate_approve_applies_concept_edits(tmp_path, adapter, pipeline_cfg):
    db = tmp_path / "runs.sqlite"
    thread_id = "on-3"
    cfg = _make_cfg(
        adapter,
        pipeline_cfg,
        run_extras={"review_plan": True},
        thread_id=thread_id,
    )
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        await graph.ainvoke(init, cfg)
        snap = await graph.aget_state(cfg)
        interrupt = next(
            i for task in snap.tasks for i in getattr(task, "interrupts", ())
        )
        concepts = list(interrupt.value["concepts"])
        concepts[0] = {**concepts[0], "script": "HOOK: edited\nCTA: now"}
        result = await graph.ainvoke(
            Command(resume={"action": "approve", "concepts": concepts}),
            cfg,
        )

    edited = next(item for item in result["results"] if item.id == concepts[0]["id"])
    assert edited.script == "HOOK: edited\nCTA: now"


async def test_review_gate_approve_edits_profile_without_replacing_media(
    tmp_path,
    adapter,
    pipeline_cfg,
):
    db = tmp_path / "runs.sqlite"
    thread_id = "on-voice-reroll"
    cfg = _make_cfg(
        adapter,
        pipeline_cfg,
        run_extras={"review_plan": True},
        thread_id=thread_id,
    )
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        await graph.ainvoke(init, cfg)

        snap = await graph.aget_state(cfg)
        all_interrupts = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
        payload = all_interrupts[0].value
        creators = payload["creators"]
        updated_creators = [
            {
                "id": creator["id"],
                "archetype": f"Reviewed archetype {idx}",
            }
            for idx, creator in enumerate(creators)
        ]

        result = await graph.ainvoke(Command(resume={
            "action": "approve",
            "creators": updated_creators,
        }), cfg)

    roster = result.get("roster", [])
    assert len(roster) == 2
    assert roster[0]["id"] == updated_creators[0]["id"]
    assert roster[0]["archetype"] == "Reviewed archetype 0"
    assert roster[0]["voice_id"] != "voice-reroll-0"
    assert roster[0]["voice_preview_uri"] != "data:audio/wav;base64,reroll-0"


async def test_review_gate_interrupt_value_structure(tmp_path, adapter, pipeline_cfg):
    db = tmp_path / "runs.sqlite"
    thread_id = "on-4"
    cfg = _make_cfg(
        adapter,
        pipeline_cfg,
        run_extras={"review_plan": True},
        thread_id=thread_id,
    )
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        await graph.ainvoke(init, cfg)
        snap = await graph.aget_state(cfg)

    via_tasks = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
    via_snap = list(getattr(snap, "interrupts", []))

    assert via_tasks or via_snap
    interrupt_obj = (via_tasks or via_snap)[0]
    assert hasattr(interrupt_obj, "value")
    assert interrupt_obj.value["type"] == "review_creative_plan"
