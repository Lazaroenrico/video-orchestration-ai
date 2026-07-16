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
from typing import Any, Optional

_log = logging.getLogger(__name__)

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

import orchestrator.feedback_store as _feedback_store
from orchestrator import media_store, stream_bus
from orchestrator.adapters._agent_loop import AgentRunResult
from orchestrator.adapters.base import VoiceProfile, assign_voice_profile
from orchestrator.config import default_media_path, default_videos_path
from orchestrator.graph.state import Artifact, Item, new_item
from orchestrator.nodes.base import as_item, get_pipeline
from orchestrator.stage_executor import StageExecutionError, execute_stage_tool
from orchestrator.tools.assembly import assemble_video_tool, upscale_video_tool
from orchestrator.tools.base import tool_context_from_config
from orchestrator.tools.concepts import generate_concepts_tool
from orchestrator.tools.creators import build_creator_tool
from orchestrator.tools.qc import qc_check_tool
from orchestrator.tools.scripts import write_script_tool
from orchestrator.tools.video import generate_clip_tool
from orchestrator.tracing import add_trace_metadata, traced

async def _build_voice_preview(
    adapter: Any, creator: dict[str, Any], *, run_id: str, media_root: Any,
) -> str | None:
    """Resolve um ``voice_preview_uri`` audível para o creator, quando possível.

    - Voz já baixada como áudio (ElevenLabs via Replicate, ``voice_source_uri`` setado por
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

    # Voz nova baixável (ex.: URL do Replicate, que expira em ~1h): persiste os
    # bytes com nome versionado por reroll — o path muda a cada troca, então o
    # <audio> da UI nunca serve cache da voz anterior.
    voice_uri = next_creator.get("voice_id")
    if isinstance(voice_uri, str) and media_store._is_downloadable(voice_uri):
        creator_id = next_creator.get("id") or "creator"
        local = await media_store.persist_media(
            voice_uri,
            Path(media_root) / run_id / creator_id,
            f"voice-r{reroll_count}",
            web_prefix=f"/media/{run_id}/{creator_id}",
        )
        if local != voice_uri:
            next_creator["voice_id"] = local
            next_creator["voice_ref"] = local
            next_creator["voice"] = local
            next_creator["voice_source_uri"] = voice_uri
            next_creator["voice_preview_uri"] = local

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


def _normalize_seed_creator(creator: dict[str, Any]) -> dict[str, Any] | None:
    """Normaliza um creator escolhido anteriormente para o contrato do fan-out."""
    creator_id = creator.get("id") or creator.get("creator_id")
    if not creator_id:
        return None
    image_uri = (
        creator.get("image_uri")
        or creator.get("image")
        or creator.get("upscaled_base")
        or creator.get("image_source_uri")
    )
    voice_ref = (
        creator.get("voice_id")
        or creator.get("voice_ref")
        or creator.get("voice")
    )
    voice_preview_uri = (
        creator.get("voice_preview_uri")
        or creator.get("voice_preview")
        or creator.get("preview_uri")
    )
    normalized = dict(creator)
    normalized["id"] = str(creator_id)
    if image_uri is not None:
        normalized["upscaled_base"] = image_uri
        normalized["image_uri"] = image_uri
        normalized["image"] = image_uri
        normalized["image_source_uri"] = creator.get("image_source_uri") or image_uri
    if voice_ref is not None:
        normalized["voice_id"] = voice_ref
        normalized["voice_ref"] = voice_ref
        normalized["voice"] = voice_ref
    if voice_preview_uri is not None:
        normalized["voice_preview_uri"] = voice_preview_uri
    normalized["angles"] = list(creator.get("angles") or [])
    return normalized


def _ensure_seed_reference_image(creator: dict[str, Any], media_root: Path) -> None:
    """Garante que a referência de imagem do creator reutilizado seja buscável pelo
    provider (Step 6, vídeo real). O fan-out usa ``image_source_uri or upscaled_base``;
    um creator vindo do store carrega só o path local ``/media/...`` (não acessível
    externamente). Reconstrói um ``data:`` URI a partir do arquivo em disco quando a
    referência atual não é http(s)/data:. No-op quando já é buscável (data:/http) ou
    quando não há arquivo local (ex.: seed de teste sem mídia). Mutação in-place."""
    ref = creator.get("image_source_uri")
    if isinstance(ref, str) and media_store._is_downloadable(ref):
        return
    for candidate in (creator.get("image_source_uri"), creator.get("upscaled_base"),
                      creator.get("image_uri"), creator.get("image")):
        data_uri = media_store.data_uri_from_media_path(candidate, media_root) if candidate else None
        if data_uri is not None:
            creator["image_source_uri"] = data_uri
            return


# ===================== Top-graph (BatchState) =====================

@traced("node.roster", run_type="chain", step=3)
async def node_roster(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 3 — constrói o roster de creators reutilizáveis (uma vez por run)."""
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    run_cfg = config["configurable"].get("run", {})
    n = int(pipeline.get("roster", {}).get("creators", 5))
    creator_prompt = run_cfg.get("creator_prompt")
    run_id = config["configurable"].get("thread_id", "run")
    media_root = default_media_path()
    add_trace_metadata(step=3, stage="roster", creators=n)

    seed_creator = run_cfg.get("seed_creator")
    if isinstance(seed_creator, dict):
        normalized_seed = _normalize_seed_creator(seed_creator)
        if normalized_seed is not None:
            _ensure_seed_reference_image(normalized_seed, media_root)
            add_trace_metadata(step=3, stage="roster", creators=1, seeded=True)
            return {"roster": [normalized_seed]}

    async def _build(i: int) -> dict[str, Any]:
        stream_bus.emit_token({
            "type": "creator_start",
            "creator_id": f"creator-{i}",
        })
        # Perfil concreto por índice: garante paridade imagem↔voz e variedade de
        # gênero no roster mesmo quando o briefing não cita gênero.
        profile = assign_voice_profile(creator_prompt, None, index=i)
        creator = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="roster",
            tool_name="build_creator",
            tool_fn=build_creator_tool,
            index=i, system_prompt=creator_prompt, voice_profile=profile,
        )
        # Baixa e persiste os bytes (imagem/voz) e reescreve as URIs para caminhos
        # locais servíveis. No-op para mock:// / voice_id (sem rede, sem disco).
        creator = await media_store.persist_creator_media(
            creator, run_id=run_id, media_root=media_root,
        )
        creator["voice_preview_uri"] = await _build_voice_preview(
            tool_ctx.adapter, creator, run_id=run_id, media_root=media_root,
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
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    run_cfg = state.get("config", {})
    offer = run_cfg.get("offer", "demo offer")
    n = int(run_cfg.get("batch_size") or pipeline.get("batch", {}).get("default_size", 12))
    seed = state.get("run_id", "run")
    # Step 10 -> 1: vés pelos hooks vencedores do ciclo anterior (fecha o loop).
    bias = run_cfg.get("prior_winning_styles") or None
    add_trace_metadata(step=1, stage="concepts", batch_size=n, offer=offer)
    concepts = await execute_stage_tool(
        config,
        tool_ctx,
        catalog_stage="concepts",
        tool_name="generate_concepts",
        tool_fn=generate_concepts_tool,
        offer=offer,
        n=n,
        seed=seed,
        bias=bias,
    )
    return {"concepts": concepts}


@traced("node.scripts", run_type="chain", step=2)
async def node_scripts(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 2 — escreve o script de cada conceito (batch-level, ANTES do creator).

    O script fica guardado em ``concept["script"]``; o creator ainda não existe, então
    ``write_script`` recebe um ``creator_ref`` genérico. O fan-out atribui o creator a
    cada item e move o script (``concept["script"]`` -> ``Item.script``) depois.
    """
    tool_ctx = tool_context_from_config(config)
    run_cfg = config["configurable"].get("run", {})
    platform = run_cfg.get("platform", "tiktok")
    concepts = state.get("concepts") or []
    add_trace_metadata(step=2, stage="scripts", batch_size=len(concepts), platform=platform)

    async def _write(concept: dict[str, Any]) -> dict[str, Any]:
        script = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="scripts",
            tool_name="write_script",
            tool_fn=write_script_tool,
            concept=concept, creator_ref="creator", platform=platform,
        )
        return {**concept, "script": script}

    # gather preserva a ordem dos conceitos; determinístico no mock.
    scripted = await asyncio.gather(*(_write(c) for c in concepts))
    return {"concepts": list(scripted)}


@traced("node.concept_review", run_type="chain", step=2)
async def node_concept_review(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Gate humano de edição de concept+script (Step 2.5). Pausa só quando run.edit_concepts.

    No resume, ``decision["concepts"]`` substitui a lista — o usuário pode ter editado
    campos/script e excluído conceitos (produção segue só com os incluídos).
    """
    run_cfg = config["configurable"].get("run", {})
    concepts = state.get("concepts") or []
    add_trace_metadata(step=2, stage="concept_review", batch_size=len(concepts))
    if not run_cfg.get("edit_concepts") or not concepts:
        return {}  # passthrough: CLI/testes inalterados
    decision = interrupt({"type": "edit_concepts", "concepts": concepts})
    edited = (decision or {}).get("concepts")
    if edited is None:
        return {}  # sem decisão explícita → mantém os conceitos como estão
    return {"concepts": list(edited)}


@traced("node.feedback", run_type="chain", step=10)
async def node_feedback(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 10 — agrega resultados (store que o Step 1 leria no próximo ciclo)."""
    results: list[Item] = state.get("results", [])
    approved = [r for r in results if r.assembled is not None and not r.dropped]
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


def _settle_takes(run: AgentRunResult) -> tuple[Artifact, float]:
    """Resolve um run de vídeo em ``(clip_final, custo_de_todas_as_takes)`` — D33.

    O agent pode gerar várias takes e só a última vira clip do item; as descartadas
    **já foram pagas**, então o custo soma todas. As descartadas ficam registradas no
    meta do clip final (proveniência auditável) em vez de irem para ``item.clips``:
    o IntegrityQC valida cada clip do item, e uma take rejeitada reprovaria o item
    inteiro além de furar ``qc.required_clip_count``.
    """
    clip: Artifact = run.result
    cost = round(sum(a.result.meta["cost_usd"] for a in run.successful), 6)
    if not run.superseded:
        return clip, cost
    meta = dict(clip.meta)
    meta["agent_takes"] = len(run.successful)
    meta["superseded_takes"] = [
        {
            "uri": a.result.uri,
            "cost_usd": a.result.meta.get("cost_usd"),
            "revision": a.call.arguments.get("revision"),
        }
        for a in run.superseded
    ]
    return clip.model_copy(update={"meta": meta}), cost


def _assembly_prompt(item: Item, run_prompt: str | None, *, platform: str) -> str:
    """Prompt para o vídeo final, usando Seedance como gerador de montagem."""
    parts: list[str] = []
    if run_prompt:
        parts.append(run_prompt.strip())
    parts.append(f"Final vertical UGC ad for {platform}.")
    parts.append("Use the creator reference image as the consistent on-camera creator.")
    parts.append("Create one polished final video from the approved script and concept.")
    if item.script:
        parts.append(f"Script:\n{item.script}")
    concept = item.concept or {}
    concept_bits = [
        f"{key}: {concept[key]}"
        for key in ("hook", "angle", "hook_style", "offer", "format")
        if concept.get(key)
    ]
    if concept_bits:
        parts.append("Concept context: " + "; ".join(concept_bits))
    parts.append("No mock footage. No placeholder frames. No captions burned into the video.")
    return "\n\n".join(parts)

def make_gen_node(tier: str):
    """Fabrica o node de geração de talking-head (Step 4) para um tier."""

    async def _gen(state: Any, config: RunnableConfig) -> dict[str, Any]:
        item = as_item(state)
        tool_ctx = tool_context_from_config(config)
        pipeline = get_pipeline(config)
        run_cfg = config["configurable"].get("run", {})
        seconds = int(pipeline.get("clip", {}).get("duration_seconds", 8))
        add_trace_metadata(
            step=4, stage="talking_head", item_id=item.id, tier=tier,
            attempt=item.attempts,
        )
        run = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="video",
            tool_name="generate_clip",
            tool_fn=generate_clip_tool,
            with_attempts=True,
            item_id=item.id, tier=tier, seconds=seconds, attempt=item.attempts,
            system_prompt=_video_prompt(
                item, run_cfg.get("video_prompt"), stage="talking-head"
            ),
            reference_image_uri=item.creator_image_uri,
            stage="talking_head",
        )
        clip, takes_cost = _settle_takes(run)
        # Surfaça se o clip veio do provider real (replicate) ou de fallback mock,
        # + o modelo e a URI de saída — responde "está gerando o vídeo mesmo?".
        add_trace_metadata(
            step=4, stage="talking_head_done", item_id=item.id,
            video_provider=clip.meta.get("provider"),
            video_model=clip.meta.get("model"),
            video_uri=clip.uri,
            fallback_reason=clip.meta.get("fallback_reason"),
            agent_takes=run.executed,
        )
        cost_usd = round(item.cost_usd + takes_cost, 4)
        run_id = config["configurable"].get("thread_id", "run")
        videos_root = default_videos_path()
        updated = item.model_copy(update={"clips": item.clips + [clip]})
        persisted = await media_store.persist_item_media(
            updated, run_id=run_id, videos_root=videos_root,
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
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    run_cfg = config["configurable"].get("run", {})
    seconds = int(pipeline.get("clip", {}).get("duration_seconds", 8))
    add_trace_metadata(step=5, stage="product_demo", item_id=item.id, attempt=item.attempts)
    run = await execute_stage_tool(
        config,
        tool_ctx,
        catalog_stage="video",
        tool_name="generate_clip",
        tool_fn=generate_clip_tool,
        with_attempts=True,
        item_id=f"{item.id}:demo", tier="ltx", seconds=seconds, attempt=item.attempts,
        system_prompt=_video_prompt(item, run_cfg.get("video_prompt"), stage="product-demo"),
        reference_image_uri=item.creator_image_uri,
        stage="product_demo",
    )
    demo, takes_cost = _settle_takes(run)
    add_trace_metadata(
        step=5, stage="product_demo_done", item_id=item.id,
        video_provider=demo.meta.get("provider"),
        video_model=demo.meta.get("model"),
        video_uri=demo.uri,
        fallback_reason=demo.meta.get("fallback_reason"),
        agent_takes=run.executed,
    )
    cost_usd = round(item.cost_usd + takes_cost, 4)
    run_id = config["configurable"].get("thread_id", "run")
    videos_root = default_videos_path()
    updated = item.model_copy(update={"clips": item.clips + [demo]})
    persisted = await media_store.persist_item_media(
        updated, run_id=run_id, videos_root=videos_root,
    )
    return {
        "clips": persisted.clips,
        "cost_usd": cost_usd,
    }


@traced("node.qc", run_type="chain", step=7)
async def node_qc(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 7 — QC determinístico; reprova incrementa attempts (alimenta o gate)."""
    item = as_item(state)
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    fail_rate = float(pipeline.get("qc", {}).get("fail_rate", 0.34))
    qc = await execute_stage_tool(
        config,
        tool_ctx,
        catalog_stage="qc",
        tool_name="qc_check",
        tool_fn=qc_check_tool,
        item=item,
        fail_rate=fail_rate,
    )
    add_trace_metadata(
        step=7, stage="qc", item_id=item.id, attempt=item.attempts,
        qc_score=qc.score, qc_passed=qc.passed,
    )
    if qc.passed:
        return {"qc": qc}
    return {"qc": qc, "attempts": item.attempts + 1}

async def _mock_assembled(item: Item, *, platform: str, system_prompt: str) -> Artifact:
    """Vídeo final mock para o fallback opt-in de assembly, marcado como degradado."""
    from orchestrator.adapters.mock import MockAdapter

    mock_art = await MockAdapter(tiers=[]).assemble(
        item=item, platform=platform, system_prompt=system_prompt,
    )
    meta = {**mock_art.meta, "provider": "mock", "fallback_reason": "assembly_gateway_rejected"}
    return mock_art.model_copy(update={"meta": meta})


@traced("node.assembly", run_type="chain", step=8)
async def node_assembly(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 8 — montagem/edição do clip aprovado em vídeo final.

    Resiliente: uma falha do assembler (ex.: gateway do Seedance recusa a imagem por
    "real person") **não mata o item**. Por padrão o item completa sem vídeo final,
    carregando os clips já gerados + ``error``; com ``assembly.allow_mock_fallback``
    ligado, degrada para um final mock marcado com ``fallback_reason``.
    """
    item = as_item(state)
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    run_cfg = config["configurable"].get("run", {})
    platform = run_cfg.get("platform", "tiktok")
    system_prompt = _assembly_prompt(item, run_cfg.get("video_prompt"), platform=platform)
    add_trace_metadata(step=8, stage="assembly", item_id=item.id, platform=platform)

    reason: Optional[str] = None
    try:
        art = await execute_stage_tool(
            config,
            tool_ctx, item=item, platform=platform, system_prompt=system_prompt,
            catalog_stage="assembly",
            tool_name="assemble_video",
            tool_fn=assemble_video_tool,
        )
    except StageExecutionError:  # erro de config, não falha do assembler → estoura alto
        raise
    except Exception as exc:  # noqa: BLE001 — assembly best-effort; falha vira erro no item
        art = None
        reason = str(exc)

    if art is None:
        allow_fallback = bool((pipeline.get("assembly") or {}).get("allow_mock_fallback", False))
        if not allow_fallback:
            add_trace_metadata(step=8, stage="assembly_failed", item_id=item.id, error=reason)
            return {"assembled": None, "error": f"assembly: {reason}"}
        art = await _mock_assembled(item, platform=platform, system_prompt=system_prompt)
        add_trace_metadata(
            step=8, stage="assembly_fallback", item_id=item.id,
            fallback_reason="assembly_gateway_rejected", error=reason,
        )

    run_id = config["configurable"].get("thread_id", "run")
    videos_root = default_videos_path()
    updated = item.model_copy(update={"assembled": art})
    persisted = await media_store.persist_item_media(
        updated, run_id=run_id, videos_root=videos_root,
    )
    return {"assembled": persisted.assembled, "error": None}


@traced("node.upscale", run_type="chain", step=8)
async def node_upscale(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Step 8 (pós-montagem) — upscale do vídeo final entregue.

    O upscale foi movido da imagem do creator para cá: roda uma vez, sobre o
    ``assembled``. Best-effort — se a montagem falhou (``assembled is None``), se o
    adapter é passthrough (uri inalterada) ou se o upscale levanta, mantém o vídeo
    montado sem derrubar o item.
    """
    item = as_item(state)
    if item.assembled is None:  # montagem não completou → nada a escalar
        return {}
    tool_ctx = tool_context_from_config(config)
    add_trace_metadata(step=8, stage="upscale", item_id=item.id)
    try:
        upscaled_uri = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="upscale",
            tool_name="upscale_video",
            tool_fn=upscale_video_tool,
            media_uri=item.assembled.uri,
        )
    except StageExecutionError:  # erro de config, não falha do upscaler → estoura alto
        raise
    except Exception as exc:  # noqa: BLE001 — upscale best-effort; preserva o montado
        add_trace_metadata(step=8, stage="upscale_failed", item_id=item.id, error=str(exc))
        return {}
    if not upscaled_uri or upscaled_uri == item.assembled.uri:
        return {}  # passthrough/no-op: nada a persistir
    # ``upscaled_from`` guarda o vídeo pré-upscale; não reuso ``source_uri`` porque o
    # persist_item_media o sobrescreve com a proveniência de download da nova uri.
    art = item.assembled.model_copy(update={
        "uri": upscaled_uri,
        "meta": {**item.assembled.meta, "upscaled": True, "upscaled_from": item.assembled.uri},
    })
    run_id = config["configurable"].get("thread_id", "run")
    updated = item.model_copy(update={"assembled": art})
    persisted = await media_store.persist_item_media(
        updated, run_id=run_id, videos_root=default_videos_path(),
    )
    add_trace_metadata(step=8, stage="upscale_done", item_id=item.id)
    return {"assembled": persisted.assembled}


@traced("node.drop", run_type="chain", step=7)
async def node_drop(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Item que esgotou as tentativas de QC: descartado, nunca publicado."""
    item = as_item(state)
    add_trace_metadata(step=7, stage="drop", item_id=item.id, dropped=True)
    return {"dropped": True}
