"""Montagem do ``StateGraph`` da pipeline (topologia fixa no v1).

- Subgrafo per-item (``Item``): script -> [route tier] -> gen(tier) -> product_demo
  -> qc -> [qc gate] -> {assembly -> distribution | regen | drop}.
- Grafo de topo (``BatchState``): roster -> concepts -> [fan-out via Send] ->
  process_item (invoca o subgrafo) -> feedback.
"""
from __future__ import annotations

from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from orchestrator.graph.routing import route_after_qc, route_after_script
from orchestrator.graph.state import BatchState, Item, new_item
from orchestrator.nodes.base import as_item, tier_names
from orchestrator.nodes.stages import (
    make_gen_node,
    node_assembly,
    node_concepts,
    node_distribution,
    node_drop,
    node_feedback,
    node_product_demo,
    node_qc,
    node_roster,
    node_script,
)


def build_item_graph(pipeline: dict[str, Any]):
    """Subgrafo per-item com as conditional edges (tier routing + QC gate)."""
    tns = tier_names(pipeline)
    max_attempts = int(pipeline.get("qc", {}).get("max_attempts", 3))
    qc_map: dict[str, str] = {t: t for t in tns}
    qc_map.update({"assembly": "assembly", "drop": "drop"})

    sg = StateGraph(Item)
    sg.add_node("script", node_script)
    for t in tns:
        sg.add_node(t, make_gen_node(t))
    sg.add_node("product_demo", node_product_demo)

    async def node_qc_and_route(state: dict[str, Any], config: RunnableConfig) -> Command:
        update = await node_qc(state, config)
        updated_item = as_item(state).model_copy(update=update)
        destination = route_after_qc(updated_item, max_attempts, tns)
        return Command(update=update, goto=destination)

    sg.add_node("qc", node_qc_and_route, destinations=qc_map)
    sg.add_node("assembly", node_assembly)
    sg.add_node("distribution", node_distribution)
    sg.add_node("drop", node_drop)

    sg.add_edge(START, "script")

    async def route_after_script_node(state: dict[str, Any]) -> str:
        return route_after_script(as_item(state), tns)

    sg.add_conditional_edges(
        "script", route_after_script_node, {t: t for t in tns}
    )
    for t in tns:
        sg.add_edge(t, "product_demo")
    sg.add_edge("product_demo", "qc")
    sg.add_edge("assembly", "distribution")
    sg.add_edge("distribution", END)
    sg.add_edge("drop", END)
    return sg.compile()


def _sub_config(config: dict[str, Any]) -> dict[str, Any]:
    """Config enxuto p/ o subgrafo: só o configurable (sem chaves de checkpoint)."""
    cfg = config.get("configurable", {})
    return {
        "configurable": {
            "adapter": cfg["adapter"],
            "pipeline": cfg["pipeline"],
            "run": cfg.get("run", {}),
        },
        "recursion_limit": config.get("recursion_limit", 50),
    }


def build_graph(pipeline: dict[str, Any], checkpointer: Optional[Any] = None):
    """Grafo de topo, com fan-out paralelo e (opcional) checkpointer p/ resume."""
    item_app = build_item_graph(pipeline)

    g = StateGraph(BatchState)
    g.add_node("roster", node_roster)
    g.add_node("concepts", node_concepts)

    async def process_item(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        result = await item_app.ainvoke(state, _sub_config(config))
        item = as_item(result)
        return {
            "results": [item],
            "total_cost_usd": item.cost_usd,
        }

    g.add_node("process_item", process_item)
    g.add_node("feedback", node_feedback)

    g.add_edge(START, "roster")
    g.add_edge("roster", "concepts")

    async def fan_out(state: dict[str, Any]) -> list[Send]:
        concepts = state.get("concepts", [])
        roster = state.get("roster") or [{}]
        sends: list[Send] = []
        for i, concept in enumerate(concepts):
            creator = roster[i % len(roster)]
            item = new_item(concept=concept, creator_ref=creator.get("id"))
            cid = concept.get("id")
            if cid:
                item.id = str(cid)
            sends.append(Send("process_item", item.model_dump()))
        return sends

    g.add_conditional_edges("concepts", fan_out, ["process_item"])
    g.add_edge("process_item", "feedback")
    g.add_edge("feedback", END)
    return g.compile(checkpointer=checkpointer)
