"""Testes da montagem e execução do grafo (mock, ponta a ponta no nível de unidade)."""
import asyncio

import pytest

from orchestrator.graph.builder import build_graph, build_item_graph, make_fan_out_node
from orchestrator.graph.state import Item


def test_item_graph_has_expected_nodes(pipeline_cfg):
    app = build_item_graph(pipeline_cfg)
    nodes = set(app.get_graph().nodes)
    for n in ("script", "ltx", "kling", "seedance", "product_demo", "qc", "assembly", "distribution", "drop"):
        assert n in nodes


def test_top_graph_has_expected_nodes(pipeline_cfg):
    app = build_graph(pipeline_cfg)
    nodes = set(app.get_graph().nodes)
    for n in ("roster", "concepts", "process_item", "feedback"):
        assert n in nodes


def test_top_graph_routes_concepts_via_conditional_send(pipeline_cfg):
    app = build_graph(pipeline_cfg)
    edges = app.get_graph().edges
    matching = [
        edge for edge in edges
        if edge.source == "concepts" and edge.target == "process_item"
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
                    "image_source_uri": "data:image/png;base64,abc",
                }
            ],
        }
    )

    assert len(sends) == 1
    item_payload = sends[0].arg
    assert item_payload["creator_ref"] == "creator-0"
    assert item_payload["creator_image_uri"] == "data:image/png;base64,abc"


async def test_item_subgraph_runs_one_item_to_distribution(adapter, pipeline_cfg):
    app = build_item_graph(pipeline_cfg)
    cfg = {"configurable": {"adapter": adapter, "pipeline": pipeline_cfg, "run": {"platform": "tiktok"}}}
    item = Item(concept={"hook": "h", "hook_style": "problem", "offer": "x"}, creator_ref="creator-0")
    out = await asyncio.wait_for(app.ainvoke(item.model_dump(), cfg), timeout=5)
    result = Item.model_validate(out)
    # terminou OU publicado OU descartado (nunca preso no meio)
    assert result.distributed or result.dropped
    assert result.script is not None
    assert result.cost_usd > 0


async def test_top_graph_fans_out_and_aggregates(run_config):
    app = build_graph(run_config["configurable"]["pipeline"])
    init = {"run_id": "t-builder", "config": {"offer": "serum", "batch_size": 6}}
    out = await asyncio.wait_for(app.ainvoke(init, run_config), timeout=5)
    results = out["results"]
    assert len(results) == 6                       # fan-out de 6 conceitos
    # cada item termina publicado ou descartado
    assert all(r.distributed or r.dropped for r in results)
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
    # itens regenerados acumulam custo de mais de um tier (escalonamento)
    regen = [r for r in out["results"] if r.attempts >= 1]
    assert all(len(r.clips) >= 2 for r in regen)
