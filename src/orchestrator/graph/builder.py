"""Montagem do ``StateGraph`` da pipeline (topologia fixa no v1).

- Subgrafo per-item (``Item``): [route tier] -> gen(tier) -> product_demo -> qc ->
  [qc gate] -> {assembly | regen | drop}. O script já vem pronto no ``Item`` (gerado
  em nível de batch antes do creator).
- Grafo de topo (``BatchState``): concepts -> scripts -> concept_review (gate de edição)
  -> roster -> approval -> [fan-out via Send] -> process_item (invoca o subgrafo) ->
  feedback.
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
    node_approval,
    node_assembly,
    node_concept_review,
    node_concepts,
    node_drop,
    node_feedback,
    node_product_demo,
    node_qc,
    node_roster,
    node_scripts,
)
from orchestrator.tracing import add_trace_metadata, traced


def build_item_graph(pipeline: dict[str, Any]):
    """Subgrafo per-item com as conditional edges (tier routing + QC gate)."""
    tns = tier_names(pipeline)
    max_attempts = int(pipeline.get("qc", {}).get("max_attempts", 3))
    qc_map: dict[str, str] = {t: t for t in tns}
    qc_map.update({"assembly": "assembly", "drop": "drop"})

    sg = StateGraph(Item)
    for t in tns:
        sg.add_node(t, make_gen_node(t))
    sg.add_node("product_demo", node_product_demo)

    sg.add_node("qc", make_qc_route_node(tns, max_attempts), destinations=qc_map)
    sg.add_node("assembly", node_assembly)
    sg.add_node("drop", node_drop)

    # O script já vem pronto no Item (batch-level, antes do creator): o subgrafo entra
    # direto no roteamento de tier.
    sg.add_conditional_edges(
        START, make_script_route_node(tns), {t: t for t in tns}
    )
    for t in tns:
        sg.add_edge(t, "product_demo")
    sg.add_edge("product_demo", "qc")
    sg.add_edge("assembly", END)
    sg.add_edge("drop", END)
    return sg.compile()


def make_qc_route_node(tns: list[str], max_attempts: int):
    """Cria o node de QC + roteamento com marcador de tracing testável."""

    @traced("node.qc.route", run_type="chain", step=7)
    async def node_qc_and_route(state: dict[str, Any], config: RunnableConfig) -> Command:
        update = await node_qc(state, config)
        updated_item = as_item(state).model_copy(update=update)
        destination = route_after_qc(updated_item, max_attempts, tns)
        add_trace_metadata(
            step=7, stage="qc_route", item_id=updated_item.id,
            destination=destination,
        )
        return Command(update=update, goto=destination)

    return node_qc_and_route


def make_script_route_node(tns: list[str]):
    """Cria o roteador pós-script com marcador de tracing testável."""

    @traced("node.script.route", run_type="chain", step=4)
    async def route_after_script_node(state: dict[str, Any]) -> str:
        return route_after_script(as_item(state), tns)

    return route_after_script_node


# Chaves de checkpoint do LangGraph: não podem vazar para o subgrafo per-item (que é
# compilado sem checkpointer próprio). Todo o resto — em especial os ``callbacks`` que
# carregam o run tree do LangSmith — PRECISA ser propagado, senão cada item vira uma
# root run solta e o grafo aparece fragmentado no LangSmith.
_CHECKPOINT_CONFIGURABLE_KEYS = frozenset(
    {"thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint_map"}
)
# Chaves de topo que ligam o run do item ao run do batch no tracing. ``run_id`` é
# deliberadamente omitido para não forçar/colidir o id da run do subgrafo.
_PROPAGATED_TOP_KEYS = ("callbacks", "tags", "metadata", "max_concurrency")


def _sub_config(config: dict[str, Any]) -> dict[str, Any]:
    """Config p/ o subgrafo per-item: preserva a linhagem de trace (callbacks/tags/
    metadata) e remove apenas as chaves de checkpoint do LangGraph."""
    cfg = config.get("configurable", {})
    sub: dict[str, Any] = {
        "configurable": {
            k: v for k, v in cfg.items() if k not in _CHECKPOINT_CONFIGURABLE_KEYS
        },
        "recursion_limit": config.get("recursion_limit", 50),
    }
    for key in _PROPAGATED_TOP_KEYS:
        if key in config:
            sub[key] = config[key]
    return sub


def make_process_item_node(item_app: Any):
    """Cria o node do fan-out por item com marcador de tracing testável."""

    @traced("node.process_item", run_type="chain", step=6)
    async def process_item(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        item_in = as_item(state)
        add_trace_metadata(step=6, stage="process_item", item_id=item_in.id)
        result = await item_app.ainvoke(state, _sub_config(config))
        item = as_item(result)
        add_trace_metadata(
            step=6, stage="process_item_done", item_id=item.id,
            cost_usd=item.cost_usd, dropped=item.dropped,
            assembled=bool(item.assembled),
        )
        return {
            "results": [item],
            "total_cost_usd": item.cost_usd,
        }

    return process_item


def make_fan_out_node():
    """Cria o fan-out de conceitos para itens com marcador de tracing testável."""

    @traced("node.fan_out", run_type="chain", step=6)
    async def fan_out(state: dict[str, Any]) -> list[Send]:
        concepts = state.get("concepts", [])
        roster = state.get("roster") or [{}]
        add_trace_metadata(step=6, stage="fan_out", items=len(concepts), roster_size=len(roster))
        sends: list[Send] = []
        for i, concept in enumerate(concepts):
            creator = roster[i % len(roster)]
            creator_image_uri = creator.get("image_source_uri") or creator.get("upscaled_base")
            # O script foi gerado em nível de batch (antes do creator) e vive em
            # concept["script"]; move-o para Item.script e mantém o concept limpo.
            concept_data = dict(concept)
            script = concept_data.pop("script", None)
            item = new_item(
                concept=concept_data,
                creator_ref=creator.get("id"),
                creator_image_uri=creator_image_uri,
                creator_image_local_path=creator.get("image_local_path"),
            )
            item.script = script
            cid = concept_data.get("id")
            if cid:
                item.id = str(cid)
            sends.append(Send("process_item", item.model_dump()))
        return sends

    return fan_out


def build_graph(pipeline: dict[str, Any], checkpointer: Optional[Any] = None):
    """Grafo de topo, com fan-out paralelo e (opcional) checkpointer p/ resume."""
    item_app = build_item_graph(pipeline)

    g = StateGraph(BatchState)
    g.add_node("concepts", node_concepts)
    g.add_node("scripts", node_scripts)
    g.add_node("concept_review", node_concept_review)
    g.add_node("roster", node_roster)
    g.add_node("approval", node_approval)

    g.add_node("process_item", make_process_item_node(item_app))
    g.add_node("feedback", node_feedback)

    # concepts -> scripts -> [gate de edição] -> creator -> [gate de aprovação] -> fan-out
    g.add_edge(START, "concepts")
    g.add_edge("concepts", "scripts")
    g.add_edge("scripts", "concept_review")
    g.add_edge("concept_review", "roster")
    g.add_edge("roster", "approval")

    g.add_conditional_edges("approval", make_fan_out_node(), ["process_item"])
    g.add_edge("process_item", "feedback")
    g.add_edge("feedback", END)
    return g.compile(checkpointer=checkpointer)
