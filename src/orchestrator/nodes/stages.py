"""Os 10 stages como nodes do LangGraph (mocks no v1).

Agrupados em um módulo por concisão; cada função abaixo corresponde a um passo do
Context.md (marcado nos comentários). Top-graph opera sobre ``BatchState``; o
subgrafo per-item opera sobre ``Item``.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

import orchestrator.feedback_store as _feedback_store
from orchestrator import media_store, stream_bus
from orchestrator.adapters.base import VoiceProfile, assign_voice_profile
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


def _wav_data_uri(*seed_parts: Any) -> str:
    """WAV PCM 8-bit mono minúsculo e determinístico para preview offline."""
    sample_rate = 4000
    n_samples = 400
    digest = hashlib.sha256("|".join(str(p) for p in seed_parts).encode()).digest()
    samples = bytes(digest[i % len(digest)] for i in range(n_samples))
    data_size = len(samples)
    header = (
        b"RIFF"
        + (36 + data_size).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + sample_rate.to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (8).to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )
    return "data:audio/wav;base64," + base64.b64encode(header + samples).decode()


def _creator_index(creator: dict[str, Any]) -> int:
    creator_id = str(creator.get("id") or "")
    match = re.search(r"(\d+)$", creator_id)
    return int(match.group(1)) if match else 0


def _creator_voice_profile(creator: dict[str, Any]) -> VoiceProfile | None:
    """Reconstrói o ``VoiceProfile`` persistido no creator, quando presente."""
    raw = creator.get("voice_profile")
    if not isinstance(raw, dict) or not raw.get("preset"):
        return None
    try:
        return VoiceProfile(preset=raw["preset"], prompt=raw.get("prompt", ""))
    except ValueError:
        return None


async def reroll_creator_voice(
    adapter: Any, creator: dict[str, Any], *, run_id: str, media_root: Any,
) -> dict[str, Any]:
    """Regenera só os metadados de voz do creator, preservando a imagem.

    O gênero (``voice_profile.preset``) é preservado: só a amostra de voz muda, então
    a voz continua casando com a imagem inalterada.
    """
    reroll_count = int(creator.get("voice_reroll_count") or 0) + 1
    profile = _creator_voice_profile(creator)
    reroll = getattr(adapter, "reroll_creator_voice", None)

    if callable(reroll):
        updated = await reroll(
            creator_id=creator.get("id"),
            index=_creator_index(creator),
            reroll_count=reroll_count,
            creator=creator,
            voice_profile=profile,
        )
        next_creator = {**creator, **updated}
    else:
        base_voice = (
            creator.get("voice_ref")
            or creator.get("voice")
            or creator.get("voice_id")
            or f"voice-{_creator_index(creator)}"
        )
        voice_ref = f"{base_voice}::reroll-{reroll_count}"
        next_creator = {
            **creator,
            "voice_id": voice_ref,
            "voice_ref": voice_ref,
            "voice": voice_ref,
            "voice_source_uri": None,
            "voice_preview_uri": _wav_data_uri(
                run_id, creator.get("id"), reroll_count,
                profile.preset if profile is not None else "",
            ),
        }
    # Trava o gênero da imagem: reroll nunca altera o preset resolvido.
    if profile is not None:
        next_creator["voice_profile"] = profile.as_dict()

    next_creator["voice_reroll_count"] = reroll_count
    next_creator["voice_preview_uri"] = await _build_voice_preview(
        adapter, next_creator, run_id=run_id, media_root=media_root,
    ) or next_creator.get("voice_preview_uri")
    return next_creator


def apply_roster_updates(
    roster: list[dict[str, Any]], updates: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Mescla updates vindos do approval resume no roster atual do grafo."""
    if not updates:
        return roster

    by_id = {
        str(update.get("id")): update
        for update in updates
        if update.get("id") is not None
    }
    merged: list[dict[str, Any]] = []
    for creator in roster:
        creator_id = str(creator.get("id") or "")
        update = by_id.get(creator_id)
        if update is None:
            merged.append(creator)
            continue

        voice_ref = update.get("voice_ref") or update.get("voice") or update.get("voice_id")
        image_uri = update.get("image_uri") or update.get("image") or update.get("upscaled_base")
        preview = (
            update.get("voice_preview_uri")
            or update.get("voice_preview")
            or update.get("preview_uri")
        )
        merged_creator = {**creator, **update}
        if voice_ref is not None:
            merged_creator["voice_id"] = voice_ref
            merged_creator["voice_ref"] = voice_ref
            merged_creator["voice"] = voice_ref
        if image_uri is not None:
            merged_creator["upscaled_base"] = image_uri
            merged_creator["image_uri"] = image_uri
            merged_creator["image"] = image_uri
        if preview is not None:
            merged_creator["voice_preview_uri"] = preview
        merged.append(merged_creator)
    return merged


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
        # Perfil concreto por índice: garante paridade imagem↔voz e variedade de
        # gênero no roster mesmo quando o briefing não cita gênero.
        profile = assign_voice_profile(creator_prompt, None, index=i)
        creator = await adapter.build_creator(
            index=i, system_prompt=creator_prompt, voice_profile=profile,
        )
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
    roster = apply_roster_updates(roster, (decision or {}).get("creators"))
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


def _video_prompt(item: Item, run_prompt: str | None, *, stage: str) -> str:
    """Prompt textual para vídeo sem áudio, usando script e conceito disponíveis."""
    parts: list[str] = []
    if run_prompt:
        parts.append(run_prompt.strip())
    parts.append(f"Generate a silent vertical UGC {stage} video.")
    if item.script:
        parts.append(f"Script context:\n{item.script}")
    concept = item.concept or {}
    concept_bits = [
        f"{key}: {concept[key]}"
        for key in ("hook", "angle", "hook_style", "offer", "format")
        if concept.get(key)
    ]
    if concept_bits:
        parts.append("Concept context: " + "; ".join(concept_bits))
    parts.append("No audio. No captions burned into the video.")
    return "\n\n".join(parts)

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
            system_prompt=_video_prompt(
                item, run_cfg.get("video_prompt"), stage="talking-head"
            ),
            reference_image_uri=item.creator_image_uri,
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
        system_prompt=_video_prompt(item, run_cfg.get("video_prompt"), stage="product-demo"),
        reference_image_uri=item.creator_image_uri,
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
