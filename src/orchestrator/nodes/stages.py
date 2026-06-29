"""Os 10 stages como nodes do LangGraph (mocks no v1).

Agrupados em um módulo por concisão; cada função abaixo corresponde a um passo do
Context.md (marcado nos comentários). Top-graph opera sobre ``BatchState``; o
subgrafo per-item opera sobre ``Item``.
"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

import orchestrator.feedback_store as _feedback_store
from orchestrator.graph.state import Item, new_item
from orchestrator.nodes.base import as_item, get_adapter, get_pipeline

# ===================== Top-graph (BatchState) =====================


async def node_roster(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 3 — constrói o roster de creators reutilizáveis (uma vez por run)."""
    adapter = get_adapter(config)
    pipeline = get_pipeline(config)
    n = int(pipeline.get("roster", {}).get("creators", 5))
    roster = [await adapter.build_creator(index=i) for i in range(n)]
    return {"roster": roster}


async def node_concepts(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 1 — gera o batch de conceitos (data-driven, spread de hooks)."""
    adapter = get_adapter(config)
    pipeline = get_pipeline(config)
    run_cfg = state.get("config", {})
    offer = run_cfg.get("offer", "demo offer")
    n = int(run_cfg.get("batch_size") or pipeline.get("batch", {}).get("default_size", 12))
    seed = state.get("run_id", "run")
    # Step 10 -> 1: vés pelos hooks vencedores do ciclo anterior (fecha o loop).
    bias = run_cfg.get("prior_winning_styles") or None
    concepts = await adapter.generate_concepts(offer=offer, n=n, seed=seed, bias=bias)
    return {"concepts": concepts}


async def node_feedback(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 10 — agrega resultados (store que o Step 1 leria no próximo ciclo)."""
    results: list[Item] = state.get("results", [])
    approved = [r for r in results if r.distributed]
    dropped = [r for r in results if r.dropped]
    summary = {
        "produced": len(results),
        "approved": len(approved),
        "dropped": len(dropped),
        "total_attempts": sum(r.attempts for r in results),
        "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
        "winning_styles": _top_styles(approved),
    }
    store_path = config["configurable"].get("feedback_store")
    if store_path:
        run_id = state.get("run_id") or ""
        _feedback_store.save_feedback(store_path, run_id, summary)
    return {"feedback": summary}


def _top_styles(items: list[Item]) -> list[str]:
    counts: dict[str, int] = {}
    for it in items:
        style = str(it.concept.get("hook_style", "unknown"))
        counts[style] = counts.get(style, 0) + 1
    return [s for s, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ===================== Subgrafo per-item (Item) =====================


async def node_script(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 2 — escreve o script no voice do creator, calibrado por plataforma."""
    item = as_item(state)
    adapter = get_adapter(config)
    run_cfg = config["configurable"].get("run", {})
    platform = run_cfg.get("platform", "tiktok")
    script = await adapter.write_script(
        concept=item.concept, creator_ref=item.creator_ref or "creator-0", platform=platform
    )
    return {"script": script}


def make_gen_node(tier: str):
    """Fabrica o node de geração de talking-head (Step 4) para um tier."""

    async def _gen(state: Any, config: RunnableConfig) -> dict[str, Any]:
        item = as_item(state)
        adapter = get_adapter(config)
        pipeline = get_pipeline(config)
        seconds = int(pipeline.get("clip", {}).get("duration_seconds", 8))
        clip = await adapter.generate_clip(
            item_id=item.id, tier=tier, seconds=seconds, attempt=item.attempts
        )
        return {
            "tier": tier,
            "clips": item.clips + [clip],
            "cost_usd": round(item.cost_usd + clip.meta["cost_usd"], 4),
        }

    _gen.__name__ = f"gen_{tier}"
    return _gen


async def node_product_demo(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 5 — clip de product demo (lean barato: LTX), anexado ao item."""
    item = as_item(state)
    adapter = get_adapter(config)
    pipeline = get_pipeline(config)
    seconds = int(pipeline.get("clip", {}).get("duration_seconds", 8))
    demo = await adapter.generate_clip(
        item_id=f"{item.id}:demo", tier="ltx", seconds=seconds, attempt=item.attempts
    )
    return {
        "clips": item.clips + [demo],
        "cost_usd": round(item.cost_usd + demo.meta["cost_usd"], 4),
    }


async def node_qc(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 7 — QC determinístico; reprova incrementa attempts (alimenta o gate)."""
    item = as_item(state)
    adapter = get_adapter(config)
    pipeline = get_pipeline(config)
    fail_rate = float(pipeline.get("qc", {}).get("fail_rate", 0.34))
    qc = await adapter.qc_check(item_id=item.id, attempt=item.attempts, fail_rate=fail_rate)
    if qc.passed:
        return {"qc": qc}
    return {"qc": qc, "attempts": item.attempts + 1}


async def node_assembly(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 8 — montagem/edição do clip aprovado em vídeo final."""
    item = as_item(state)
    adapter = get_adapter(config)
    run_cfg = config["configurable"].get("run", {})
    platform = run_cfg.get("platform", "tiktok")
    art = await adapter.assemble(item_id=item.id, platform=platform)
    return {"assembled": art}


async def node_distribution(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 9 — agenda o vídeo no portfolio de contas."""
    item = as_item(state)
    adapter = get_adapter(config)
    await adapter.distribute(item_id=item.id)
    return {"distributed": True}


async def node_drop(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Item que esgotou as tentativas de QC: descartado, nunca publicado."""
    return {"dropped": True}
