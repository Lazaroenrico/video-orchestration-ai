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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

import orchestrator.feedback_store as _feedback_store
from orchestrator import media_store, stream_bus
from orchestrator.adapters.base import RenderedMedia, VoiceProfile, assign_voice_profile
from orchestrator.config import default_media_path, default_videos_path
from orchestrator.creative_contracts import (
    CampaignInput,
    CreatorRoster,
    ScriptResult,
    script_result_from_text,
)
from orchestrator.graph.state import Artifact, FailureDetail, Item
from orchestrator.nodes.base import as_item, get_pipeline
from orchestrator.stage_executor import StageExecutionError, execute_stage_tool
from orchestrator.storage.base import is_downloadable
from orchestrator.storage.resolve import resolve_signed_uris
from orchestrator.storage.retention import (
    RETENTION_INTERMEDIATE,
    RETENTION_KEEP,
    RETENTION_REJECTED,
)
from orchestrator.tools.assembly import (
    assemble_video_tool,
    synthesize_voiceover_tool,
    upscale_video_tool,
)
from orchestrator.tools.base import tool_context_from_config
from orchestrator.tools.concepts import generate_concepts_tool
from orchestrator.tools.creator_profiles import design_creator_roster_tool
from orchestrator.tools.creators import (
    build_creator_tool,
    derive_creator_voice_spec_tool,
    design_creator_voice_tool,
    finalize_creator_voice_tool,
)
from orchestrator.tools.qc import qc_check_tool
from orchestrator.tools.scripts import write_script_tool
from orchestrator.tools.video import VideoEffectError, generate_clip_tool
from orchestrator.tracing import add_trace_metadata, traced

_log = logging.getLogger(__name__)


async def _report_creative_progress(
    config: RunnableConfig,
    *,
    stage_id: str,
    completed_units: int,
    total_units: int,
) -> None:
    try:
        await adispatch_custom_event(
            "creative_progress",
            {
                "stage_id": stage_id,
                "completed_units": completed_units,
                "total_units": total_units,
            },
            config=config,
        )
    except RuntimeError as exc:
        if "without a parent run id" not in str(exc):
            raise

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
    if is_downloadable(voice_ref):
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
    if isinstance(voice_uri, str) and is_downloadable(voice_uri):
        creator_id = next_creator.get("id") or "creator"
        local = await media_store.persist_media(
            voice_uri,
            Path(media_root) / run_id / creator_id,
            f"voice-r{reroll_count}",
            web_prefix=f"/media/{run_id}/{creator_id}",
        )
        if local != voice_uri:
            next_creator["voice_id"] = local
            stable_voice_ref = next_creator.get("voice_model_ref")
            next_creator["voice_ref"] = stable_voice_ref or local
            next_creator["voice"] = stable_voice_ref or local
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


_REVIEW_CONCEPT_FIELDS = frozenset({
    "id",
    "offer",
    "hook",
    "angle",
    "audience_problem",
    "product_mechanism",
    "evidence_basis",
    "format",
    "hook_style",
    "script",
    "script_draft",
})
_EDITABLE_CONCEPT_FIELDS = _REVIEW_CONCEPT_FIELDS - {
    "id",
    "offer",
    "script_draft",
}
_REVIEW_CREATOR_FIELDS = frozenset({
    "id",
    "archetype",
    "visual_brief",
    "voice_brief",
    "performance_style",
    "exclusions",
    "image_uri",
    "voice_ref",
    "voice_preview_uri",
    "selected_voice_candidate_id",
    "image",
    "voice",
    "angles",
    "run_id",
    "offer",
    "status",
})
_EDITABLE_CREATOR_FIELDS = frozenset({
    "archetype",
    "visual_brief",
    "voice_brief",
    "performance_style",
    "exclusions",
    "selected_voice_candidate_id",
})


def apply_review_concept_updates(
    concepts: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply user-editable copy while preserving server-owned identity and shape."""
    existing_ids = [str(concept.get("id") or "") for concept in concepts]
    update_ids = [str(update.get("id") or "") for update in updates]
    if (
        not all(existing_ids)
        or len(update_ids) != len(set(update_ids))
        or set(update_ids) != set(existing_ids)
    ):
        raise ValueError("review must preserve the same concept IDs")

    by_id: dict[str, dict[str, Any]] = {}
    for update in updates:
        unknown = set(update) - _REVIEW_CONCEPT_FIELDS
        if unknown:
            raise ValueError(
                "unsupported concept review fields: "
                + ", ".join(sorted(unknown))
            )
        by_id[str(update["id"])] = update

    return [
        {
            **concept,
            **{
                key: value
                for key, value in by_id[str(concept["id"])].items()
                if key in _EDITABLE_CONCEPT_FIELDS
            },
        }
        for concept in concepts
    ]


def apply_review_creator_updates(
    roster: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply casting direction only; media pointers and IDs remain server-owned."""
    existing_ids = [str(creator.get("id") or "") for creator in roster]
    update_ids = [str(update.get("id") or "") for update in updates]
    if (
        len(existing_ids) != 2
        or not all(existing_ids)
        or len(update_ids) != len(set(update_ids))
        or set(update_ids) != set(existing_ids)
    ):
        raise ValueError("review must preserve exactly two creator IDs")

    by_id: dict[str, dict[str, Any]] = {}
    for update in updates:
        unknown = set(update) - _REVIEW_CREATOR_FIELDS
        if unknown:
            raise ValueError(
                "unsupported creator review fields: "
                + ", ".join(sorted(unknown))
            )
        by_id[str(update["id"])] = update

    reviewed: list[dict[str, Any]] = []
    for creator in roster:
        update = by_id[str(creator["id"])]
        merged = {
            **creator,
            **{
                key: value
                for key, value in update.items()
                if key in _EDITABLE_CREATOR_FIELDS
            },
        }
        brief_changed = (
            "voice_brief" in update
            and update["voice_brief"] != creator.get("voice_brief")
        )
        if brief_changed:
            previous_candidates = list(creator.get("voice_candidates") or [])
            history = list(creator.get("voice_design_history") or [])
            if previous_candidates:
                previous_batch = creator.get("voice_design_batch")
                history.append(
                    dict(previous_batch)
                    if isinstance(previous_batch, dict)
                    else previous_candidates
                )
            merged["voice_design_history"] = history
            merged["voice_candidates"] = []
            merged.pop("voice_design_batch", None)
            merged["selected_voice_candidate_id"] = None
        elif "selected_voice_candidate_id" in update:
            selected_id = str(update.get("selected_voice_candidate_id") or "")
            candidate_ids = {
                str(candidate.get("candidate_id") or "")
                for candidate in creator.get("voice_candidates") or []
                if isinstance(candidate, dict)
            }
            if selected_id and selected_id not in candidate_ids:
                raise ValueError(
                    "selected voice candidate must belong to creator "
                    + str(creator.get("id") or "unknown")
                )
        reviewed.append(merged)
    return reviewed


def validate_voice_selections(roster: list[dict[str, Any]]) -> None:
    """Require one current candidate selected for every reviewed creator."""
    for creator in roster:
        selected_id = str(creator.get("selected_voice_candidate_id") or "")
        candidate_ids = {
            str(candidate.get("candidate_id") or "")
            for candidate in creator.get("voice_candidates") or []
            if isinstance(candidate, dict)
        }
        if not selected_id or selected_id not in candidate_ids:
            creator_id = str(creator.get("id") or "unknown")
            raise ValueError(
                f"select one voice candidate belonging to creator {creator_id}"
            )


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
    if isinstance(ref, str) and is_downloadable(ref):
        return
    for candidate in (creator.get("image_source_uri"), creator.get("upscaled_base"),
                      creator.get("image_uri"), creator.get("image")):
        data_uri = media_store.data_uri_from_media_path(candidate, media_root) if candidate else None
        if data_uri is not None:
            creator["image_source_uri"] = data_uri
            return


async def classify_item_retention(
    item: Item,
    *,
    db: Any,
    now: datetime,
) -> None:
    """Aplica a retenção da D30 aos clips de um item, uma vez que seu destino é conhecido.

    Não dá para classificar no momento da persistência: ali o QC ainda não rodou. Só
    depois do veredito sabemos qual take é o entregável.

    - Item **aprovado**: a última take é o clip aprovado (retido); as anteriores foram
      superadas e viram tentativas intermediárias (2 dias).
    - Item **descartado**: todas as takes são clips reprovados (3 dias).
    - Item **ainda em voo** (QC reprovou mas há tentativa pela frente): nada a fazer —
      condenar bytes que a próxima rodada pode promover seria cedo demais.

    Clips sem ponteiro de storage (``mock://``, que nunca virou objeto) são pulados.
    """
    if db is None:
        return

    if item.dropped:
        targets = [(clip, RETENTION_REJECTED) for clip in item.clips]
    elif item.qc is not None and item.qc.passed and item.clips:
        *superseded, final = item.clips
        targets = [(clip, RETENTION_INTERMEDIATE) for clip in superseded]
        targets.append((final, RETENTION_KEEP))
    else:
        return

    for clip, retention_class in targets:
        storage_key = (clip.meta or {}).get("storage_key")
        if storage_key:
            await db.set_retention(storage_key, retention_class, now=now)


def _persistence(config: RunnableConfig, *, storage_key: str) -> dict[str, Any]:
    """Backend de storage + DB de artifacts resolvidos para o run (D30).

    Ambos vêm do ``configurable`` (montado uma vez em ``runner._build_config``). Quando
    ausentes — configs montados à mão em teste — o ``media_store`` cai no disco local a
    partir do root, que é o comportamento histórico.
    """
    configurable = config["configurable"]
    return {
        "storage": configurable.get(storage_key),
        "db": configurable.get("artifact_db"),
    }


# ===================== Top-graph (BatchState) =====================


def _prompt_with_persona(persona: Any, prompt: Any) -> str | None:
    persona_text = persona.strip() if isinstance(persona, str) else ""
    prompt_text = prompt.strip() if isinstance(prompt, str) else ""
    if persona_text and prompt_text:
        return f"{persona_text}\n\n{prompt_text}"
    if persona_text:
        return persona_text
    if prompt_text:
        return prompt_text
    return None


def _campaign_input(
    state: dict[str, Any],
    config: RunnableConfig,
) -> CampaignInput:
    raw = state.get("campaign")
    if isinstance(raw, dict):
        return CampaignInput.model_validate(raw)
    state_config = state.get("config") if isinstance(state.get("config"), dict) else {}
    run_config = config["configurable"].get("run", {})
    return CampaignInput(
        offer=str(state_config.get("offer") or "demo offer"),
        audience=str(run_config.get("audience") or "General adult audience"),
        facts_restrictions=run_config.get("facts_restrictions"),
        creator_direction=run_config.get("creator_direction")
        or run_config.get("creator_prompt"),
        video_direction=run_config.get("video_direction")
        or run_config.get("video_prompt"),
        platform=run_config.get("platform", "tiktok"),
        objective=run_config.get("objective", "conversion"),
        batch_size=int(
            state_config.get("batch_size")
            or config["configurable"].get("pipeline", {}).get("batch", {}).get(
                "default_size", 12
            )
        ),
        performance=run_config.get("performance"),
    )


@traced("node.roster", run_type="chain", step=3)
async def node_roster(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 3 — constrói o roster de creators reutilizáveis (uma vez por run)."""
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    run_cfg = config["configurable"].get("run", {})
    profiles = state.get("creator_profiles") or []
    n = len(profiles) if profiles else int(pipeline.get("roster", {}).get("creators", 2))
    run_id = config["configurable"].get("thread_id", "run")
    media_root = default_media_path()
    add_trace_metadata(step=3, stage="roster", creators=n)

    seed_creator = run_cfg.get("seed_creator")
    if isinstance(seed_creator, dict):
        normalized_seed = _normalize_seed_creator(seed_creator)
        if normalized_seed is not None:
            _ensure_seed_reference_image(normalized_seed, media_root)
            add_trace_metadata(step=3, stage="roster", creators=1, seeded=True)
            seed_id = str(normalized_seed["id"])
            return {
                "roster": [normalized_seed],
                "creator_assignments": [
                    {
                        "concept_id": str(concept.get("id") or ""),
                        "creator_id": seed_id,
                    }
                    for concept in state.get("concepts") or []
                    if isinstance(concept, dict) and concept.get("id")
                ],
            }

    preview_completed = 0
    preview_progress_lock = asyncio.Lock()

    async def _build(i: int) -> dict[str, Any]:
        stream_bus.emit_token({
            "type": "creator_start",
            "creator_id": f"creator-{i}",
        })
        # Perfil concreto por índice: garante paridade imagem↔voz e variedade de
        # gênero no roster mesmo quando o briefing não cita gênero.
        creative_profile = profiles[i] if i < len(profiles) else {}
        profile_prompt = "\n".join(
            value
            for value in (
                creative_profile.get("visual_brief"),
                creative_profile.get("voice_brief"),
                creative_profile.get("performance_style"),
            )
            if isinstance(value, str) and value.strip()
        ) or _prompt_with_persona(state.get("persona"), run_cfg.get("creator_prompt"))
        profile = assign_voice_profile(profile_prompt, None, index=i)
        creator = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="roster",
            tool_name="build_creator",
            tool_fn=build_creator_tool,
            index=i, system_prompt=profile_prompt, voice_profile=profile,
        )
        if creative_profile:
            creator = {**creator, **creative_profile, "id": creative_profile["id"]}

        # Baixa e persiste os bytes (imagem/voz) e reescreve as URIs para caminhos
        # locais servíveis. No-op para mock:// / voice_id (sem rede, sem disco).
        creator = await media_store.persist_creator_media(
            creator, run_id=run_id, media_root=media_root,
            **_persistence(config, storage_key="media_storage"),
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
        nonlocal preview_completed
        async with preview_progress_lock:
            preview_completed += 1
            await _report_creative_progress(
                config,
                stage_id="creator_previews",
                completed_units=preview_completed,
                total_units=n,
            )
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


@traced("node.voice_candidates", run_type="chain", step=3)
async def node_voice_candidates(
    state: dict[str, Any],
    config: RunnableConfig,
) -> dict[str, Any]:
    """Deriva o voice spec e gera previews depois que as imagens do roster existem."""
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    review_enabled = bool(config["configurable"].get("run", {}).get("review_plan"))
    revision = state.get("revision_request") or {}
    is_voice_reroll = revision.get("target") == "voices"
    requested_ids = {str(value) for value in revision.get("ids") or []}
    roster = list(state.get("roster") or [])
    roster_ids = {str(creator.get("id") or "") for creator in roster}
    if is_voice_reroll:
        if not requested_ids or not requested_ids <= roster_ids:
            raise ValueError("voice reroll IDs must belong to the current creator roster")
    voice_cfg = pipeline.get("voice", {})
    max_rerolls = int(voice_cfg.get("max_rerolls_per_creator", 2))
    updated_roster: list[dict[str, Any]] = []
    design_cost = 0.0
    for creator in roster:
        creator_dict = dict(creator)
        creator_id = str(creator_dict.get("id") or "creator")
        if is_voice_reroll and creator_id not in requested_ids:
            updated_roster.append(creator_dict)
            continue
        reroll_count = int(creator_dict.get("voice_reroll_count") or 0)
        if is_voice_reroll:
            if reroll_count >= max_rerolls:
                raise ValueError(
                    f"voice reroll limit reached for creator {creator_id}"
                )
            reroll_count += 1

        voice_spec = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="voice_spec",
            tool_name="derive_creator_voice_spec",
            tool_fn=derive_creator_voice_spec_tool,
            profile=creator_dict,
        )
        voice_batch = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="voice_candidates",
            tool_name="design_creator_voice",
            tool_fn=design_creator_voice_tool,
            spec=voice_spec,
            creator_id=creator_id,
            reroll_count=reroll_count,
        )
        batch_dict = (
            voice_batch.model_dump(mode="json")
            if hasattr(voice_batch, "model_dump")
            else dict(voice_batch)
        )
        candidates = [dict(candidate) for candidate in batch_dict.get("candidates") or []]
        run_id = str(state.get("run_id") or config["configurable"].get("thread_id") or "run")
        candidates = await media_store.persist_voice_candidates(
            candidates,
            run_id=run_id,
            creator_id=creator_id,
            design_hash=str(batch_dict.get("description_hash") or "unknown"),
            media_root=default_media_path(),
            **_persistence(config, storage_key="media_storage"),
        )
        batch_dict["candidates"] = candidates
        batch_dict["reroll_count"] = reroll_count
        design_cost += float(batch_dict.get("cost_usd") or 0.0)
        if is_voice_reroll:
            previous_candidates = list(creator_dict.get("voice_candidates") or [])
            history = list(creator_dict.get("voice_design_history") or [])
            if previous_candidates:
                previous_batch = creator_dict.get("voice_design_batch")
                history.append(
                    dict(previous_batch)
                    if isinstance(previous_batch, dict)
                    else previous_candidates
                )
            creator_dict["voice_design_history"] = history
            creator_dict["voice_reroll_count"] = reroll_count
            for key in ("voice_ref", "voice_id", "voice"):
                creator_dict.pop(key, None)
        creator_dict["voice_spec"] = (
            voice_spec.model_dump(mode="json")
            if hasattr(voice_spec, "model_dump")
            else dict(voice_spec)
        )
        creator_dict["voice_candidates"] = candidates
        creator_dict["voice_design_batch"] = batch_dict
        if review_enabled:
            creator_dict["selected_voice_candidate_id"] = None
        elif (
            candidates
            and pipeline.get("voice", {}).get("selection_without_review", "first")
            == "first"
        ):
            creator_dict["selected_voice_candidate_id"] = candidates[0]["candidate_id"]
        if candidates:
            creator_dict["voice_preview_uri"] = candidates[0]["preview"]["uri"]
        updated_roster.append(creator_dict)
    return {"roster": updated_roster, "total_cost_usd": design_cost}

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
    campaign = _campaign_input(state, config)
    offer = campaign.offer
    n = campaign.batch_size
    seed = state.get("run_id", "run")
    # Step 10 -> 1: vés pelos hooks vencedores do ciclo anterior (fecha o loop).
    run_cfg = state.get("config", {})
    bias = run_cfg.get("prior_winning_styles") or None
    revision = state.get("revision_request") or {}
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
        campaign=campaign.model_dump(mode="json"),
        revision_feedback=revision.get("feedback"),
    )
    return {
        "campaign": campaign.model_dump(mode="json"),
        "concepts": concepts,
    }


@traced("node.scripts", run_type="chain", step=2)
async def node_scripts(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Step 2 — escreve o script de cada conceito (batch-level, ANTES do creator).

    O script fica guardado em ``concept["script"]``; o creator ainda não existe, então
    ``write_script`` recebe um ``creator_ref`` genérico. O fan-out atribui o creator a
    cada item e move o script (``concept["script"]`` -> ``Item.script``) depois.
    """
    tool_ctx = tool_context_from_config(config)
    campaign = _campaign_input(state, config)
    pipeline = config["configurable"].get("pipeline") or {}
    assembly_cfg = pipeline.get("assembly", {})
    narration_target = assembly_cfg.get("narration_target_seconds")
    narration_min_words = assembly_cfg.get("narration_min_words", 28)
    narration_max_words = assembly_cfg.get("narration_max_words")
    platform = campaign.platform
    concepts = state.get("concepts") or []
    completed = 0
    progress_lock = asyncio.Lock()
    script_concurrency = max(
        1, int((pipeline.get("batch") or {}).get("max_concurrency", 8))
    )
    script_semaphore = asyncio.Semaphore(script_concurrency)
    add_trace_metadata(step=2, stage="scripts", batch_size=len(concepts), platform=platform)

    async def _write(concept: dict[str, Any]) -> dict[str, Any]:
        async with script_semaphore:
            result = await execute_stage_tool(
                config,
                tool_ctx,
                catalog_stage="scripts",
                tool_name="write_script",
                tool_fn=write_script_tool,
                concept=concept, creator_ref="creator", platform=platform,
                campaign=campaign.model_dump(mode="json"),
                revision_feedback=(state.get("revision_request") or {}).get("feedback"),
                return_contract=True,
                target_duration_seconds=narration_target,
                min_spoken_words=narration_min_words,
                max_spoken_words=narration_max_words,
            )
        if not isinstance(result, ScriptResult):
            if isinstance(result, str):
                result = script_result_from_text(
                    result,
                    run_id=str(state.get("run_id") or "run"),
                    concept_id=str(concept["id"]),
                )
            else:
                result = ScriptResult.model_validate(result)
        scripted = {
            **concept,
            "script": result.script,
            "script_draft": result.script_draft.model_dump(mode="json"),
        }
        nonlocal completed
        async with progress_lock:
            completed += 1
            await _report_creative_progress(
                config,
                stage_id="scripts",
                completed_units=completed,
                total_units=len(concepts),
            )
        return scripted

    # gather preserva a ordem dos conceitos; determinístico no mock.
    scripted = await asyncio.gather(*(_write(c) for c in concepts))
    return {"concepts": list(scripted)}


@traced("node.creator_profiles", run_type="chain", step=3)
async def node_creator_profiles(
    state: dict[str, Any],
    config: RunnableConfig,
) -> dict[str, Any]:
    """Create two typed casting profiles before rendering their media previews."""
    tool_ctx = tool_context_from_config(config)
    campaign = _campaign_input(state, config)
    concepts = state.get("concepts") or []
    add_trace_metadata(step=3, stage="creator_profiles", creators=2)
    result = await execute_stage_tool(
        config,
        tool_ctx,
        catalog_stage="creator_profiles",
        tool_name="design_creator_roster",
        tool_fn=design_creator_roster_tool,
        campaign=campaign.model_dump(mode="json"),
        concept_ids=[str(concept["id"]) for concept in concepts],
        creative_packages=concepts,
        revision_feedback=(state.get("revision_request") or {}).get("feedback"),
    )
    if not isinstance(result, CreatorRoster):
        result = CreatorRoster.model_validate(result)
    return {
        "creator_profiles": [
            profile.model_dump(mode="json")
            for profile in result.creators
        ],
        "creator_assignments": [
            assignment.model_dump(mode="json")
            for assignment in result.assignments
        ],
    }


@traced("node.review", run_type="chain", step=3)
async def node_review(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Single human gate for creative packages and rendered creator previews."""
    run_cfg = config["configurable"].get("run", {})
    if not run_cfg.get("review_plan", False):
        return {"review_approved": True, "revision_request": {}}
    decision = interrupt(
        {
            "type": "review_creative_plan",
            "concepts": state.get("concepts") or [],
            "creators": state.get("roster") or [],
        }
    ) or {}
    action = decision.get("action", "approve")
    if action == "regenerate":
        target = decision.get("target")
        if target not in {"concepts", "scripts", "creators", "voices"}:
            raise ValueError("regenerate target must be concepts, scripts, creators, or voices")
        result: dict[str, Any] = {
            "review_approved": False,
            "revision_request": {
                "target": target,
                "ids": list(decision.get("ids") or []),
                "feedback": str(decision.get("feedback") or ""),
            },
        }
        creators = decision.get("creators")
        if target in {"creators", "voices"} and isinstance(creators, list):
            result["roster"] = apply_review_creator_updates(
                state.get("roster") or [],
                creators,
            )
        concepts = decision.get("concepts")
        if target in {"concepts", "scripts"} and isinstance(concepts, list):
            result["concepts"] = apply_review_concept_updates(
                state.get("concepts") or [],
                concepts,
            )
        return result
    if action != "approve":
        raise ValueError("review action must be approve or regenerate")
    concepts = decision.get("concepts")
    creators = decision.get("creators")
    reviewed_roster = (
        apply_review_creator_updates(state.get("roster") or [], creators)
        if isinstance(creators, list)
        else state.get("roster", [])
    )
    validate_voice_selections(reviewed_roster)
    return {
        "concepts": (
            apply_review_concept_updates(state.get("concepts") or [], concepts)
            if isinstance(concepts, list)
            else state.get("concepts", [])
        ),
        "roster": reviewed_roster,
        "review_approved": True,
        "revision_request": {},
    }


@traced("node.finalize_voices", run_type="chain", step=3)
async def node_finalize_voices(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Permanently create only the voice candidate approved for each creator."""
    tool_ctx = tool_context_from_config(config)
    roster = list(state.get("roster") or [])
    updated_roster: list[dict[str, Any]] = []
    org_id = config["configurable"].get("organization_id", "default")

    for creator in roster:
        creator_dict = dict(creator)
        if creator_dict.get("voice_ref"):
            creator_dict.setdefault("voice_id", creator_dict["voice_ref"])
            updated_roster.append(creator_dict)
            continue

        creator_id = str(creator_dict.get("id") or "")
        candidate_id = str(creator_dict.get("selected_voice_candidate_id") or "")
        candidates = list(creator_dict.get("voice_candidates") or [])
        selected = next(
            (
                candidate
                for candidate in candidates
                if isinstance(candidate, dict)
                and str(candidate.get("candidate_id") or "") == candidate_id
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                f"creator {creator_id or 'unknown'} has no selected voice candidate"
            )
        batch = creator_dict.get("voice_design_batch")
        if not isinstance(batch, dict):
            raise ValueError(f"creator {creator_id or 'unknown'} has no voice design batch")

        finalized = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="finalize_voices",
            tool_name="finalize_creator_voice",
            tool_fn=finalize_creator_voice_tool,
            candidate_id=candidate_id,
            batch=batch,
            creator_id=creator_id,
            organization_id=org_id,
        )
        voice_ref = str(
            finalized.get("voice_ref")
            if isinstance(finalized, dict)
            else getattr(finalized, "voice_ref", "")
        ).strip()
        if not voice_ref:
            raise ValueError(f"creator {creator_id or 'unknown'} returned empty voice_ref")

        preview = selected.get("preview")
        preview_uri = (
            preview.get("uri")
            if isinstance(preview, dict)
            else getattr(preview, "uri", None)
        )
        if not preview_uri:
            raise ValueError(f"creator {creator_id or 'unknown'} has no voice preview URI")

        creator_dict["voice_ref"] = voice_ref
        creator_dict["voice_id"] = voice_ref
        creator_dict["voice"] = voice_ref
        creator_dict["voice_preview_uri"] = str(preview_uri)
        creator_dict["voice_provider"] = str(
            finalized.get("provider") or batch.get("provider") or "elevenlabs"
        )
        creator_dict["voice_design_model"] = str(
            finalized.get("design_model") or batch.get("design_model") or ""
        )
        creator_dict["voice_tts_model"] = str(finalized.get("tts_model") or "")
        creator_dict["voice_design_hash"] = str(
            batch.get("description_hash") or ""
        )
        creator_dict["voice_status"] = "selected"
        updated_roster.append(creator_dict)

    required_creator_ids = {
        str(assignment.get("creator_id") or "")
        for assignment in state.get("creator_assignments") or []
        if isinstance(assignment, dict)
    } or {str(creator.get("id") or "") for creator in roster}
    finalized_by_id = {
        str(creator.get("id") or ""): creator for creator in updated_roster
    }
    missing = sorted(
        creator_id
        for creator_id in required_creator_ids
        if not finalized_by_id.get(creator_id, {}).get("voice_ref")
    )
    if missing:
        raise ValueError("assigned creators without voice_ref: " + ", ".join(missing))

    return {"roster": updated_roster}


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
        async with _feedback_store.open_repository(store_path) as repository:
            await repository.save_feedback(run_id, summary)
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


def _video_failure_update(
    item: Item,
    exc: VideoEffectError,
    *,
    stage: str,
) -> dict[str, Any]:
    """Convert only an expected paid-video failure into an item terminal state."""
    failure = FailureDetail(
        code=exc.code,
        type=exc.error_type,
        message=str(exc),
        stage=stage,
        provider=exc.provider,
        item_id=item.id,
        effect_key=exc.effect_key,
        retryable=exc.retryable,
        uncertain=exc.uncertain,
    )
    add_trace_metadata(
        stage=stage,
        item_id=item.id,
        failure_code=failure.code,
        failure_type=failure.type,
        uncertain=failure.uncertain,
    )
    return {
        "clips": item.clips,
        "cost_usd": item.cost_usd,
        "error": failure.message,
        "failure": failure,
    }

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
        try:
            clip = await execute_stage_tool(
                config,
                tool_ctx,
                catalog_stage="video",
                tool_name="generate_clip",
                tool_fn=generate_clip_tool,
                item_id=item.id, tier=tier, seconds=seconds, attempt=item.attempts,
                system_prompt=_video_prompt(
                    item, run_cfg.get("video_prompt"), stage="talking-head"
                ),
                reference_image_uri=item.creator_image_uri,
                audio_uri=item.voiceover.uri if item.voiceover else None,
                stage="talking_head",
            )
        except VideoEffectError as exc:
            return _video_failure_update(item, exc, stage="talking_head")
        takes_cost = float(clip.meta.get("cost_usd", 0.0))
        # Surfaça se o clip veio do provider real ou de fallback mock,
        # + o modelo e a URI de saída — responde "está gerando o vídeo mesmo?".
        add_trace_metadata(
            step=4, stage="talking_head_done", item_id=item.id,
            video_provider=clip.meta.get("provider"),
            video_model=clip.meta.get("model"),
            video_uri=clip.uri,
            fallback_reason=clip.meta.get("fallback_reason"),
            video_takes=1,
        )
        cost_usd = round(item.cost_usd + takes_cost, 4)
        run_id = config["configurable"].get("thread_id", "run")
        videos_root = default_videos_path()
        updated = item.model_copy(update={"clips": item.clips + [clip]})
        persisted = await media_store.persist_item_media(
            updated, run_id=run_id, videos_root=videos_root,
            **_persistence(config, storage_key="videos_storage"),
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
    """Step 5 — clip de product demo no tier configurado, anexado ao item."""
    item = as_item(state)
    tool_ctx = tool_context_from_config(config)
    pipeline = get_pipeline(config)
    run_cfg = config["configurable"].get("run", {})
    seconds = int(pipeline.get("clip", {}).get("duration_seconds", 8))
    product_demo_tier = str(
        pipeline.get("video", {}).get("product_demo_tier")
        or next((tier["name"] for tier in pipeline.get("tiers", [])), "ltx")
    )
    add_trace_metadata(
        step=5,
        stage="product_demo",
        item_id=item.id,
        attempt=item.attempts,
        tier=product_demo_tier,
    )
    try:
        demo = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="video",
            tool_name="generate_clip",
            tool_fn=generate_clip_tool,
            item_id=f"{item.id}:demo",
            tier=product_demo_tier,
            seconds=seconds,
            attempt=item.attempts,
            system_prompt=_video_prompt(item, run_cfg.get("video_prompt"), stage="product-demo"),
            reference_image_uri=item.creator_image_uri,
            stage="product_demo",
        )
    except VideoEffectError as exc:
        return _video_failure_update(item, exc, stage="product_demo")
    takes_cost = float(demo.meta.get("cost_usd", 0.0))
    add_trace_metadata(
        step=5, stage="product_demo_done", item_id=item.id,
        video_provider=demo.meta.get("provider"),
        video_model=demo.meta.get("model"),
        video_uri=demo.uri,
        fallback_reason=demo.meta.get("fallback_reason"),
        video_takes=1,
    )
    cost_usd = round(item.cost_usd + takes_cost, 4)
    run_id = config["configurable"].get("thread_id", "run")
    videos_root = default_videos_path()
    updated = item.model_copy(update={"clips": item.clips + [demo]})
    persisted = await media_store.persist_item_media(
        updated, run_id=run_id, videos_root=videos_root,
        **_persistence(config, storage_key="videos_storage"),
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
        # Destino conhecido: a última take é o entregável, as anteriores foram superadas.
        await classify_item_retention(
            item.model_copy(update={"qc": qc}),
            db=config["configurable"].get("artifact_db"),
            now=datetime.now(timezone.utc),
        )
        return {"qc": qc}
    return {"qc": qc, "attempts": item.attempts + 1}


def _narration_text(script: str) -> str:
    """Render only spoken copy, without internal HOOK/BODY/CTA labels."""
    spoken: list[str] = []
    for raw_line in script.splitlines():
        label, separator, content = raw_line.partition(":")
        if separator and label.strip().casefold() in {"hook", "body", "cta"}:
            value = content.strip()
        else:
            value = raw_line.strip()
        if value:
            spoken.append(value)
    return " ".join(spoken)


@traced("node.voiceover", run_type="chain", step="voiceover")
async def node_voiceover(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Synthesize and persist the approved narration only after media QC passes."""
    item = as_item(state)
    voice_ref = (item.creator_voice_ref or "").strip()
    text = _narration_text(item.script or "")
    if not voice_ref:
        return {
            "voiceover": None,
            "error": "voiceover: approved creator voice is missing",
        }
    if not text:
        return {
            "voiceover": None,
            "error": "voiceover: approved script is missing",
        }

    tool_ctx = tool_context_from_config(config)
    add_trace_metadata(
        step="voiceover",
        stage="voiceover",
        item_id=item.id,
        characters=len(text),
    )
    try:
        art = await execute_stage_tool(
            config,
            tool_ctx,
            catalog_stage="voiceover",
            tool_name="synthesize_voiceover",
            tool_fn=synthesize_voiceover_tool,
            voice_ref=voice_ref,
            text=text,
            item_id=item.id,
        )
    except StageExecutionError:
        raise
    except Exception as exc:  # noqa: BLE001 - paid TTS failure must be explicit
        add_trace_metadata(
            step="voiceover",
            stage="voiceover_failed",
            item_id=item.id,
            error=str(exc),
        )
        return {"voiceover": None, "error": f"voiceover: {exc}"}

    cost_usd = round(
        item.cost_usd + float(art.meta.get("cost_usd", 0.0)),
        6,
    )
    updated = item.model_copy(
        update={"voiceover": art, "cost_usd": cost_usd, "error": None}
    )
    persisted = await media_store.persist_item_media(
        updated,
        run_id=config["configurable"].get("thread_id", "run"),
        videos_root=default_videos_path(),
        **_persistence(config, storage_key="videos_storage"),
    )
    return {
        "voiceover": persisted.voiceover,
        "cost_usd": persisted.cost_usd,
        "error": None,
    }


async def _mock_assembled(item: Item, *, platform: str, system_prompt: str) -> Artifact:
    """Vídeo final mock para o fallback opt-in de assembly, marcado como degradado."""
    from orchestrator.adapters.mock import MockAdapter

    mock_art = await MockAdapter(tiers=[]).assemble(
        item=item, platform=platform, system_prompt=system_prompt,
    )
    meta = {**mock_art.meta, "provider": "mock", "fallback_reason": "assembly_gateway_rejected"}
    return mock_art.model_copy(update={"meta": meta})


def _resolve_local_assembly_paths(item: Item) -> Item:
    """Map public ``/videos`` URLs back to guarded runtime paths for FFmpeg."""
    root = default_videos_path().resolve()

    def _artifact(artifact: Artifact | None) -> Artifact | None:
        if artifact is None or not artifact.uri.startswith("/videos/"):
            return artifact
        relative = artifact.uri.removeprefix("/videos/")
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return artifact
        return artifact.model_copy(update={"uri": str(candidate)})

    return item.model_copy(
        update={
            "clips": [_artifact(clip) for clip in item.clips],
            "voiceover": _artifact(item.voiceover),
        }
    )


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
        assembly_payload = await resolve_signed_uris(
            item.model_dump(mode="json"),
            storage=config["configurable"].get("videos_storage"),
        )
        assembly_item = _resolve_local_assembly_paths(
            Item.model_validate(assembly_payload)
        )
        art = await execute_stage_tool(
            config,
            tool_ctx, item=assembly_item, platform=platform, system_prompt=system_prompt,
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
    cost_usd = round(item.cost_usd + float(art.meta.get("cost_usd", 0.0)), 4)
    if isinstance(art, RenderedMedia):
        canonical_art = await media_store.persist_artifact_bytes(
            art.data,
            run_id=run_id,
            item_id=item.id,
            basename="assembled",
            kind="video",
            content_type=art.content_type,
            meta=art.meta,
            videos_root=videos_root,
            **_persistence(config, storage_key="videos_storage"),
        )
    else:
        canonical_art = art
    updated = item.model_copy(
        update={"assembled": canonical_art, "cost_usd": cost_usd}
    )
    persisted = await media_store.persist_item_media(
        updated, run_id=run_id, videos_root=videos_root,
        **_persistence(config, storage_key="videos_storage"),
    )
    return {
        "assembled": persisted.assembled,
        "cost_usd": persisted.cost_usd,
        "error": None,
    }


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
        **_persistence(config, storage_key="videos_storage"),
    )
    add_trace_metadata(step=8, stage="upscale_done", item_id=item.id)
    return {"assembled": persisted.assembled}


@traced("node.drop", run_type="chain", step=7)
async def node_drop(state: Any, config: RunnableConfig) -> dict[str, Any]:
    """Item que esgotou as tentativas de QC: descartado, nunca publicado."""
    item = as_item(state)
    add_trace_metadata(step=7, stage="drop", item_id=item.id, dropped=True)
    # Esgotou as tentativas: todas as takes são clips reprovados (3 dias, D30).
    await classify_item_retention(
        item.model_copy(update={"dropped": True}),
        db=config["configurable"].get("artifact_db"),
        now=datetime.now(timezone.utc),
    )
    return {"dropped": True}
