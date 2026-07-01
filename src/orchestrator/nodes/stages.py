"""Os 10 stages como nodes do LangGraph (mocks no v1).

Agrupados em um módulo por concisão; cada função abaixo corresponde a um passo do
Context.md (marcado nos comentários). Top-graph opera sobre ``BatchState``; o
subgrafo per-item opera sobre ``Item``.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

import orchestrator.feedback_store as _feedback_store
from orchestrator import media_store, stream_bus
from orchestrator.config import default_media_path
from orchestrator.graph.state import Item, new_item
from orchestrator.nodes.base import as_item, get_adapter, get_pipeline
from orchestrator.tracing import add_trace_metadata, traced

async def _build_voice_preview(
    adapter: Any, creator: dict[str, Any], *, run_id: str, media_root: Any,
) -> str | None:
    """Resolve um ``voice_preview_uri`` audível para o creator, quando possível.

    - Voz já baixada como áudio (Replicate bark, ``voice_source_uri`` setado por
      ``persist_creator_media``) -> o próprio caminho local já servível é o preview.
    - Voz opaca (ElevenLabs ``voice_id``) -> sintetiza uma amostra curta via
      ``adapter.voice.synthesize_preview`` (quando o sub-adapter existe) e persiste.
    - Preview já fornecido pelo adapter (ex.: mock emite ``data:audio/wav``): é
      preservado como está — não sobrescrevemos uma amostra audível já pronta.
    - Sem sub-adapter de voz (mock, ou falha na síntese): ``None``, no-op — não
      quebra a suíte offline.
    """
    existing = creator.get("voice_preview_uri")
    if isinstance(existing, str) and existing:
        return existing
    voice_ref = creator.get("voice_id")
    if not isinstance(voice_ref, str) or not voice_ref:
        return None
    if creator.get("voice_source_uri"):
        return voice_ref
    if media_store._is_downloadable(voice_ref):
        return None

    synth = getattr(getattr(adapter, "voice", None), "synthesize_preview", None)
    if synth is None:
        return None
    try:
        audio = await synth(voice_ref)
    except Exception as exc:  # noqa: BLE001 — preview é best-effort
        _log.error(
            "voice preview falhou (%s): %s: %s", creator.get("id"), type(exc).__name__, exc,
        )
        return None

    creator_id = creator.get("id") or "creator"
    dest_dir = Path(media_root) / run_id / creator_id
    web_prefix = f"/media/{run_id}/{creator_id}"
    return await media_store.persist_bytes(audio, dest_dir, "voice_preview", web_prefix=web_prefix)


# ===================== Top-graph (BatchState) =====================

@traced("node.roster", run_type="chain", step=3)
async def node_roster(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 3 — constrói o roster de creators reutilizáveis (uma vez por run)."""
    adapter = get_adapter(config)
    pipeline = get_pipeline(config)
    run_cfg = config["configurable"].get("run", {})
    n = int(pipeline.get("roster", {}).get("creators", 5))
    creator_prompt = run_cfg.get("creator_prompt")
    run_id = config["configurable"].get("thread_id", "run")
    media_root = default_media_path()
    add_trace_metadata(step=3, stage="roster", creators=n)

    async def _build(i: int) -> dict[str, Any]:
        stream_bus.emit_token({
            "type": "creator_start",
            "creator_id": f"creator-{i}",
        })
        creator = await adapter.build_creator(index=i, system_prompt=creator_prompt)
        # Baixa e persiste os bytes (imagem/voz) e reescreve as URIs para caminhos
        # locais servíveis. No-op para mock:// / voice_id (sem rede, sem disco).
        creator = await media_store.persist_creator_media(
            creator, run_id=run_id, media_root=media_root,
        )
        creator["voice_preview_uri"] = await _build_voice_preview(
            adapter, creator, run_id=run_id, media_root=media_root,
        )
        # Emite assim que cada creator fica pronto, com a mídia real (imagem + voz),
        # para feedback imediato na UI. No-op fora do contexto de streaming web.
        stream_bus.emit_token({
            "type": "creator_ready",
            "creator": {
                "id": creator.get("id"),
                "image": creator.get("upscaled_base"),
                "voice": creator.get("voice_id"),
                "voice_preview_uri": creator.get("voice_preview_uri"),
            },
        })
        return creator

    # return_exceptions=True evita que a falha de 1 creator cancele os siblings.
    # Errors são logados individualmente para diagnóstico; roster parcial é aceito
    # desde que ao menos 1 creator tenha sido construído com sucesso.
    results = await asyncio.gather(*(_build(i) for i in range(n)), return_exceptions=True)
    roster: list[dict[str, Any]] = []
    errors: list[tuple[int, BaseException]] = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            errors.append((i, r))
        else:
            roster.append(r)

    if errors:
        for idx, exc in errors:
            _log.error("build_creator[%d] falhou: %s: %s", idx, type(exc).__name__, exc)
        if not roster:
            raise errors[0][1]

    return {"roster": roster}

@traced("node.approval", run_type="chain", step=3)
async def node_approval(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Gate humano após o roster (Step 3.5). Pausa só quando run.approve_creators."""
    run_cfg = config["configurable"].get("run", {})
    roster = state.get("roster") or []
    add_trace_metadata(step=3, stage="approval", roster_size=len(roster))
    if not run_cfg.get("approve_creators") or not roster:
        return {}  # passthrough: CLI/testes inalterados
    payload = {
        "type": "approve_creators",
        "creators": [
            {
                "id": c.get("id"),
                "image": c.get("upscaled_base"),
                "voice": c.get("voice_id"),
                "voice_preview_uri": c.get("voice_preview_uri"),
            }
            for c in roster
        ],
    }
    decision = interrupt(payload)  # re-roda no resume; tudo acima é side-effect free
    approved_list = (decision or {}).get("approved")
    # None = nenhuma decisão → aprova todos; [] = seleção explicitamente vazia → rejeita todos
    if approved_list is None:
        approved = {c.get("id") for c in roster}
    else:
        approved = set(approved_list)
    return {"roster": [c for c in roster if c.get("id") in approved]}

@traced("node.concepts", run_type="chain", step=1)
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
    add_trace_metadata(step=1, stage="concepts", batch_size=n, offer=offer)
    concepts = await adapter.generate_concepts(offer=offer, n=n, seed=seed, bias=bias)
    return {"concepts": concepts}


@traced("node.feedback", run_type="chain", step=10)
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
    add_trace_metadata(step=10, stage="feedback", **summary)
    return {"feedback": summary}


def _top_styles(items: list[Item]) -> list[str]:
    counts: dict[str, int] = {}
    for it in items:
        style = str(it.concept.get("hook_style", "unknown"))
        counts[style] = counts.get(style, 0) + 1
    return [s for s, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ===================== Subgrafo per-item (Item) =====================

@traced("node.script", run_type="chain", step=2)
async def node_script(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 2 — escreve o script no voice do creator, calibrado por plataforma."""
    item = as_item(state)
    adapter = get_adapter(config)
    run_cfg = config["configurable"].get("run", {})
    platform = run_cfg.get("platform", "tiktok")
    add_trace_metadata(step=2, stage="script", item_id=item.id, platform=platform)
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
        run_cfg = config["configurable"].get("run", {})
        seconds = int(pipeline.get("clip", {}).get("duration_seconds", 8))
        add_trace_metadata(
            step=4, stage="talking_head", item_id=item.id, tier=tier,
            attempt=item.attempts,
        )
        clip = await adapter.generate_clip(
            item_id=item.id, tier=tier, seconds=seconds, attempt=item.attempts,
            system_prompt=run_cfg.get("video_prompt"),
        )
        cost_usd = round(item.cost_usd + clip.meta["cost_usd"], 4)
        run_id = config["configurable"].get("thread_id", "run")
        media_root = default_media_path()
        updated = item.model_copy(update={"clips": item.clips + [clip]})
        persisted = await media_store.persist_item_media(
            updated, run_id=run_id, media_root=media_root,
        )
        return {
            "tier": tier,
            "clips": persisted.clips,
            "cost_usd": cost_usd,
        }

    _gen.__name__ = f"gen_{tier}"
    return traced(f"node.video.{tier}", run_type="chain", step=4, tier=tier)(_gen)


@traced("node.product_demo", run_type="chain", step=5)
async def node_product_demo(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 5 — clip de product demo (lean barato: LTX), anexado ao item."""
    item = as_item(state)
    adapter = get_adapter(config)
    pipeline = get_pipeline(config)
    run_cfg = config["configurable"].get("run", {})
    seconds = int(pipeline.get("clip", {}).get("duration_seconds", 8))
    add_trace_metadata(step=5, stage="product_demo", item_id=item.id, attempt=item.attempts)
    demo = await adapter.generate_clip(
        item_id=f"{item.id}:demo", tier="ltx", seconds=seconds, attempt=item.attempts,
        system_prompt=run_cfg.get("video_prompt"),
    )
    cost_usd = round(item.cost_usd + demo.meta["cost_usd"], 4)
    run_id = config["configurable"].get("thread_id", "run")
    media_root = default_media_path()
    updated = item.model_copy(update={"clips": item.clips + [demo]})
    persisted = await media_store.persist_item_media(
        updated, run_id=run_id, media_root=media_root,
    )
    return {
        "clips": persisted.clips,
        "cost_usd": cost_usd,
    }


@traced("node.qc", run_type="chain", step=7)
async def node_qc(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 7 — QC determinístico; reprova incrementa attempts (alimenta o gate)."""
    item = as_item(state)
    adapter = get_adapter(config)
    pipeline = get_pipeline(config)
    fail_rate = float(pipeline.get("qc", {}).get("fail_rate", 0.34))
    qc = await adapter.qc_check(item_id=item.id, attempt=item.attempts, fail_rate=fail_rate)
    add_trace_metadata(
        step=7, stage="qc", item_id=item.id, attempt=item.attempts,
        qc_score=qc.score, qc_passed=qc.passed,
    )
    if qc.passed:
        return {"qc": qc}
    return {"qc": qc, "attempts": item.attempts + 1}

@traced("node.assembly", run_type="chain", step=8)
async def node_assembly(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 8 — montagem/edição do clip aprovado em vídeo final."""
    item = as_item(state)
    adapter = get_adapter(config)
    run_cfg = config["configurable"].get("run", {})
    platform = run_cfg.get("platform", "tiktok")
    add_trace_metadata(step=8, stage="assembly", item_id=item.id, platform=platform)
    art = await adapter.assemble(item_id=item.id, platform=platform)
    run_id = config["configurable"].get("thread_id", "run")
    media_root = default_media_path()
    updated = item.model_copy(update={"assembled": art})
    persisted = await media_store.persist_item_media(
        updated, run_id=run_id, media_root=media_root,
    )
    return {"assembled": persisted.assembled}


@traced("node.distribution", run_type="chain", step=9)
async def node_distribution(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 9 — agenda o vídeo no portfolio de contas."""
    item = as_item(state)
    adapter = get_adapter(config)
    add_trace_metadata(step=9, stage="distribution", item_id=item.id)
    await adapter.distribute(item_id=item.id)
    return {"distributed": True}


@traced("node.drop", run_type="chain", step=7)
async def node_drop(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Item que esgotou as tentativas de QC: descartado, nunca publicado."""
    item = as_item(state)
    add_trace_metadata(step=7, stage="drop", item_id=item.id, dropped=True)
    return {"dropped": True}
