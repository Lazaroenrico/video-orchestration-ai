"""TDD — node_approval gate humano (Seção B do plano).

Dois cenários:
1. OFF (default): sem run.approve_creators → passthrough, roster intacto.
2. ON: run.approve_creators=True → grafo para, interrupt com payload correto;
   resume com subset aprovado → estado final tem roster filtrado.
"""
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
# Gate ON — interrupt + resume
# ---------------------------------------------------------------------------


async def test_approval_gate_on_pauses_at_interrupt(tmp_path, adapter, pipeline_cfg):
    """Com approve_creators=True o grafo pausa com interrupt e snap.next não vazio."""
    db = tmp_path / "runs.sqlite"
    thread_id = "on-1"
    cfg = _make_cfg(adapter, pipeline_cfg, run_extras={"approve_creators": True}, thread_id=thread_id)
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        result = await graph.ainvoke(init, cfg)

        snap = await graph.aget_state(cfg)

    # O grafo deve ter parado (next não vazio)
    assert snap.next, "esperado snap.next não vazio (interrupt)"
    # Há um interrupt
    all_interrupts = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
    assert all_interrupts, "esperado pelo menos um interrupt"
    payload = all_interrupts[0].value
    assert payload["type"] == "approve_creators"
    assert "creators" in payload
    assert len(payload["creators"]) == pipeline_cfg["roster"]["creators"]


async def test_approval_gate_on_resume_filters_roster(tmp_path, adapter, pipeline_cfg):
    """Resume com subset aprovado → roster final só tem os aprovados."""
    db = tmp_path / "runs.sqlite"
    thread_id = "on-2"
    cfg = _make_cfg(adapter, pipeline_cfg, run_extras={"approve_creators": True}, thread_id=thread_id)
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        # Primeira invocação: pausa no interrupt
        await graph.ainvoke(init, cfg)

        snap = await graph.aget_state(cfg)
        all_interrupts = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
        payload = all_interrupts[0].value
        creators = payload["creators"]

        # Aprova só o primeiro creator
        approved = [creators[0]["id"]]
        result = await graph.ainvoke(Command(resume={"approved": approved}), cfg)

    roster = result.get("roster", [])
    assert len(roster) == 1
    assert roster[0]["id"] == approved[0]


async def test_approval_gate_on_resume_empty_approved(tmp_path, adapter, pipeline_cfg):
    """Resume com lista vazia de aprovados → roster vazio."""
    db = tmp_path / "runs.sqlite"
    thread_id = "on-3"
    cfg = _make_cfg(adapter, pipeline_cfg, run_extras={"approve_creators": True}, thread_id=thread_id)
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        await graph.ainvoke(init, cfg)

        snap = await graph.aget_state(cfg)
        all_interrupts = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
        assert all_interrupts, "interrupt esperado"

        result = await graph.ainvoke(Command(resume={"approved": []}), cfg)

    roster = result.get("roster", [])
    assert roster == []


async def test_approval_gate_on_resume_uses_updated_roster_state(tmp_path, adapter, pipeline_cfg):
    """Resume pode substituir metadados do roster pendente antes de confirmar aprovados."""
    db = tmp_path / "runs.sqlite"
    thread_id = "on-voice-reroll"
    cfg = _make_cfg(adapter, pipeline_cfg, run_extras={"approve_creators": True}, thread_id=thread_id)
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
                **creator,
                "voice_ref": f"voice-reroll-{idx}",
                "voice_preview_uri": f"data:audio/wav;base64,reroll-{idx}",
            }
            for idx, creator in enumerate(creators)
        ]

        result = await graph.ainvoke(
            Command(
                resume={
                    "approved": [updated_creators[0]["id"]],
                    "creators": updated_creators,
                }
            ),
            cfg,
        )

    roster = result.get("roster", [])
    assert len(roster) == 1
    assert roster[0]["id"] == updated_creators[0]["id"]
    assert roster[0]["voice_id"] == "voice-reroll-0"
    assert roster[0]["voice_preview_uri"] == "data:audio/wav;base64,reroll-0"


async def test_approval_gate_interrupt_value_structure(tmp_path, adapter, pipeline_cfg):
    """Confirma atributo exato do interrupt no LangGraph instalado."""
    db = tmp_path / "runs.sqlite"
    thread_id = "on-4"
    cfg = _make_cfg(adapter, pipeline_cfg, run_extras={"approve_creators": True}, thread_id=thread_id)
    init = {"run_id": thread_id, "config": {"offer": "serum", "batch_size": 2}}

    async with open_checkpointer(str(db)) as cp:
        graph = build_graph(pipeline_cfg, checkpointer=cp)
        await graph.ainvoke(init, cfg)
        snap = await graph.aget_state(cfg)

    # snap.tasks[*].interrupts[*].value deve funcionar
    via_tasks = [i for t in snap.tasks for i in getattr(t, "interrupts", ())]
    # snap.interrupts também deve funcionar (campo direto no StateSnapshot)
    via_snap = list(getattr(snap, "interrupts", []))

    # Pelo menos uma das abordagens deve ter encontrado o interrupt
    assert via_tasks or via_snap, "interrupt não encontrado por nenhum caminho"

    # O .value deve ser o payload de approve_creators
    interrupt_obj = (via_tasks or via_snap)[0]
    assert hasattr(interrupt_obj, "value")
    assert interrupt_obj.value["type"] == "approve_creators"
