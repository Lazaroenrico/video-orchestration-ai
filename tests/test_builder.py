"""Testes da montagem e execução do grafo (mock, ponta a ponta no nível de unidade)."""
import asyncio

import pytest

from orchestrator.graph.builder import (
    _sub_config,
    build_graph,
    build_item_graph,
    make_fan_out_node,
)
from orchestrator.graph.state import Item


def test_sub_config_propagates_trace_lineage_and_drops_checkpoint_keys():
    """O subgrafo per-item precisa herdar os callbacks do run pai para aninhar no
    LangSmith; só as chaves de checkpoint do LangGraph devem ser removidas."""
    sentinel_callbacks = object()
    parent = {
        "configurable": {
            "adapter": "A",
            "pipeline": {"x": 1},
            "run": {"platform": "tiktok"},
            "feedback_store": "fb.json",
            "thread_id": "run-123",
            "checkpoint_ns": "ns",
            "checkpoint_id": "cid",
        },
        "callbacks": sentinel_callbacks,
        "tags": ["ugc"],
        "metadata": {"run_id": "run-123"},
        "recursion_limit": 100,
    }

    sub = _sub_config(parent)

    # linhagem de trace preservada → o item aninha sob o run do batch no LangSmith
    assert sub["callbacks"] is sentinel_callbacks
    assert sub["tags"] == ["ugc"]
    assert sub["metadata"] == {"run_id": "run-123"}
    # configurable essencial mantido
    assert sub["configurable"]["adapter"] == "A"
    assert sub["configurable"]["pipeline"] == {"x": 1}
    assert sub["configurable"]["run"] == {"platform": "tiktok"}
    # chaves de checkpoint NÃO vazam para o subgrafo (roda sem checkpointer próprio)
    for k in ("thread_id", "checkpoint_ns", "checkpoint_id"):
        assert k not in sub["configurable"]


def test_item_graph_has_expected_nodes(pipeline_cfg):
    app = build_item_graph(pipeline_cfg)
    nodes = set(app.get_graph().nodes)
    for n in ("ltx", "kling", "seedance", "product_demo", "qc", "assembly", "drop"):
        assert n in nodes
    # O script agora é gerado em nível de batch (antes do creator); o subgrafo
    # per-item recebe o Item com o script já pronto, sem um node "script".
    assert "script" not in nodes
    assert "distribution" not in nodes


def test_top_graph_has_expected_nodes(pipeline_cfg):
    app = build_graph(pipeline_cfg)
    nodes = set(app.get_graph().nodes)
    for n in (
        "concepts", "scripts", "concept_review", "roster", "approval",
        "process_item", "feedback",
    ):
        assert n in nodes


def test_top_graph_orders_scripts_and_review_before_creator(pipeline_cfg):
    app = build_graph(pipeline_cfg)
    edges = {(e.source, e.target) for e in app.get_graph().edges}
    # concepts -> scripts -> concept_review (gate) -> roster (creator) -> approval
    assert ("concepts", "scripts") in edges
    assert ("scripts", "concept_review") in edges
    assert ("concept_review", "roster") in edges
    assert ("roster", "approval") in edges


def test_top_graph_routes_approval_via_conditional_send(pipeline_cfg):
    app = build_graph(pipeline_cfg)
    edges = app.get_graph().edges
    matching = [
        edge for edge in edges
        if edge.source == "approval" and edge.target == "process_item"
    ]
    assert len(matching) == 1
    assert matching[0].conditional is True


async def test_fan_out_attaches_creator_image_uri_from_roster():
    fan_out = make_fan_out_node()
    sends = await fan_out(
        {
            "concepts": [{"id": "concept-1", "hook": "h"}],
            "roster": [
                {
                    "id": "creator-0",
                    "upscaled_base": "/media/run/creator-0/image.png",
                    "image_local_path": "/tmp/run/creator-0/image.png",
                    "image_source_uri": "data:image/png;base64,abc",
                }
            ],
        }
    )

    assert len(sends) == 1
    item_payload = sends[0].arg
    assert item_payload["creator_ref"] == "creator-0"
    assert item_payload["creator_image_uri"] == "data:image/png;base64,abc"
    assert item_payload["creator_image_local_path"] == "/tmp/run/creator-0/image.png"


async def test_fan_out_moves_concept_script_into_item():
    """O script gerado em batch (concept["script"]) vira Item.script; concept fica limpo."""
    fan_out = make_fan_out_node()
    sends = await fan_out(
        {
            "concepts": [{"id": "concept-1", "hook": "h", "script": "HOOK: ...\nCTA: ..."}],
            "roster": [{"id": "creator-0"}],
        }
    )
    assert len(sends) == 1
    item_payload = sends[0].arg
    assert item_payload["script"] == "HOOK: ...\nCTA: ..."
    # o campo script não deve vazar de volta para dentro do concept
    assert "script" not in item_payload["concept"]


async def test_item_subgraph_runs_one_item_to_assembly(adapter, pipeline_cfg):
    app = build_item_graph(pipeline_cfg)
    cfg = {"configurable": {"adapter": adapter, "pipeline": pipeline_cfg, "run": {"platform": "tiktok"}}}
    # O script já vem pronto no Item (gerado antes do creator); o subgrafo o preserva.
    item = Item(
        concept={"hook": "h", "hook_style": "problem", "offer": "x"},
        creator_ref="creator-0",
        script="HOOK: h\nBODY: ...\nCTA: ...",
    )
    out = await asyncio.wait_for(app.ainvoke(item.model_dump(), cfg), timeout=5)
    result = Item.model_validate(out)
    # terminou montado OU descartado (nunca preso no meio)
    assert result.assembled is not None or result.dropped
    assert result.script == "HOOK: h\nBODY: ...\nCTA: ..."
    assert result.cost_usd > 0


async def test_top_graph_fans_out_and_aggregates(run_config):
    app = build_graph(run_config["configurable"]["pipeline"])
    init = {"run_id": "t-builder", "config": {"offer": "serum", "batch_size": 6}}
    out = await asyncio.wait_for(app.ainvoke(init, run_config), timeout=5)
    results = out["results"]
    assert len(results) == 6                       # fan-out de 6 conceitos
    # cada item termina montado ou descartado
    assert all(r.assembled is not None or r.dropped for r in results)
    # custo total = soma dos itens, > 0
    assert out["total_cost_usd"] == pytest.approx(sum(r.cost_usd for r in results))
    assert out["total_cost_usd"] > 0
    # feedback (Step 10) agregado
    fb = out["feedback"]
    assert fb["produced"] == 6
    assert fb["approved"] + fb["dropped"] == 6


async def test_qc_loop_is_exercised(run_config):
    # Com fail_rate=0.34, ao menos um item deve ter regenerado (attempts >= 1).
    app = build_graph(run_config["configurable"]["pipeline"])
    init = {"run_id": "t-loop", "config": {"offer": "serum", "batch_size": 12}}
    out = await asyncio.wait_for(app.ainvoke(init, run_config), timeout=5)
    assert any(r.attempts >= 1 for r in out["results"])
    # itens regenerados acumulam mais clips, mas continuam no tier LTX
    regen = [r for r in out["results"] if r.attempts >= 1]
    assert all(len(r.clips) >= 2 for r in regen)


async def test_qc_loop_regenerates_only_on_ltx(run_config):
    app = build_graph(run_config["configurable"]["pipeline"])
    init = {"run_id": "t-loop-ltx-only", "config": {"offer": "serum", "batch_size": 12}}
    out = await asyncio.wait_for(app.ainvoke(init, run_config), timeout=5)

    regen = [r for r in out["results"] if r.attempts >= 1]
    assert regen
    assert all(
        clip.meta.get("tier") == "ltx"
        for item in regen
        for clip in item.clips
    )
