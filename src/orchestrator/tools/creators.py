"""Creator-building tools."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from orchestrator import media_store
from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters.elevenlabs_voice_design import (
    DEFAULT_PREVIEW_TEXT,
    voice_description_hash,
)
from orchestrator.config import default_media_path
from orchestrator.tools.base import (
    ToolContext,
    direct_elevenlabs_voice_enabled,
    execute_paid_effect,
    is_paid_creator_adapter,
    require_dict,
)
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.build_creator",
    run_type="tool",
    tool_name="build_creator",
    role="creator",
    stage="roster",
)
async def build_creator_tool(
    ctx: ToolContext,
    *,
    index: int,
    system_prompt: Optional[str] = None,
    voice_profile: Optional[VoiceProfile] = None,
    media_root: Optional[str | Path] = None,
    storage: Optional[Any] = None,
    db: Optional[Any] = None,
) -> dict[str, Any]:
    add_trace_metadata(
        tool_name="build_creator",
        role="creator",
        stage="roster",
        run_id=ctx.run_id,
    )

    effective_media_root = (
        media_root
        if (media_root is not None or storage is not None)
        else default_media_path()
    )

    async def _build() -> dict[str, Any]:
        creator = await ctx.adapter.build_creator(
            index=index,
            system_prompt=system_prompt,
            voice_profile=voice_profile,
        )
        creator_dict = require_dict(creator, tool_name="build_creator_tool")
        return await media_store.persist_creator_media(
            creator_dict,
            run_id=ctx.run_id,
            media_root=effective_media_root,
            storage=storage,
            db=db,
        )

    if not is_paid_creator_adapter(ctx):
        return await _build()

    creator_id = f"creator-{index}"
    gender_token = (
        voice_profile.preset.encode("utf-8")
        if voice_profile and voice_profile.preset
        else b""
    )
    prompt_bytes = (system_prompt or "").encode("utf-8")
    prompt_hash = hashlib.sha256(prompt_bytes + gender_token).hexdigest()[:16]

    image_adapter = getattr(ctx.adapter, "image", None)
    model = str(
        getattr(image_adapter, "model", None)
        or ctx.pipeline.get("creator", {}).get("image_model")
        or ctx.pipeline.get("image", {}).get("model")
        or "gpt-image-2"
    )

    return await execute_paid_effect(
        ctx,
        effect_key=f"creator-image:{ctx.run_id}:{creator_id}:{prompt_hash}",
        provider="openai_image_units",
        units=1,
        request={
            "creator_id": creator_id,
            "index": index,
            "prompt_hash": prompt_hash,
            "gender": voice_profile.preset if voice_profile else None,
            "model": model,
        },
        operation=_build,
    )


@traced(
    "tool.derive_creator_voice_spec",
    run_type="tool",
    tool_name="derive_creator_voice_spec",
    role="llm",
    stage="voice_spec",
)
async def derive_creator_voice_spec_tool(
    ctx: ToolContext,
    *,
    profile: dict[str, Any],
    visual_brief: Optional[str] = None,
    language_code: str = "pt-BR",
) -> dict[str, Any]:
    add_trace_metadata(
        tool_name="derive_creator_voice_spec",
        role="llm",
        stage="voice_spec",
        run_id=ctx.run_id,
    )
    import re

    from orchestrator.creative_contracts import CreatorVoiceSpec

    voice_prof = profile.get("voice_profile") or {}
    preset = voice_prof.get("preset") if isinstance(voice_prof, dict) else getattr(voice_prof, "preset", None)

    voice_brief = (profile.get("voice_brief") or "").casefold()
    perf_style = (profile.get("performance_style") or "").casefold()
    archetype = (profile.get("archetype") or "").casefold()
    vis_brief = (profile.get("visual_brief") or visual_brief or "").casefold()

    _FEMALE_TOKENS = ("female", "woman", "mulher", "feminina", "feminino", "girl", "moça", "garota", "ela", "her", "women")
    _MALE_TOKENS = ("male", "man", "homem", "masculino", "boy", "rapaz", "moço", "garoto", "ele", "his", "men")

    all_text = " ".join([voice_brief, perf_style, archetype, vis_brief])

    if preset == "female":
        vocal_pres = "feminine"
    elif preset == "male":
        vocal_pres = "masculine"
    elif any(token in all_text for token in _FEMALE_TOKENS):
        vocal_pres = "feminine"
    elif any(token in all_text for token in _MALE_TOKENS):
        vocal_pres = "masculine"
    else:
        creator_id = str(profile.get("id") or "")
        match = re.search(r"(\d+)$", creator_id)
        idx = int(match.group(1)) if match else 0
        vocal_pres = "feminine" if idx % 2 == 0 else "masculine"

    if "young" in voice_brief or "jovem" in voice_brief:
        vocal_age = "young_adult"
    elif "mature" in voice_brief or "madura" in voice_brief:
        vocal_age = "mature"
    else:
        vocal_age = "adult"

    if "energetic" in perf_style or "entusiasmada" in perf_style:
        energy = "high"
        pace = "energetic"
    elif "calm" in perf_style or "calma" in perf_style:
        energy = "low"
        pace = "calm"
    else:
        energy = "balanced"
        pace = "conversational"

    spec = CreatorVoiceSpec(
        language_code=language_code,
        accent="neutral",
        vocal_presentation=vocal_pres,
        vocal_age=vocal_age,
        timbre="warm" if "warm" in voice_brief or "quente" in voice_brief else "clear",
        pace=pace,
        energy=energy,
        warmth=0.7,
        expressiveness=0.6,
        rationale=f"Derived from profile for {profile.get('id', 'creator')}",
    )
    return spec.model_dump()


@traced(
    "tool.design_creator_voice",
    run_type="tool",
    tool_name="design_creator_voice",
    role="creator",
    stage="voice_candidates",
)
async def design_creator_voice_tool(
    ctx: ToolContext,
    *,
    spec: dict[str, Any],
    preview_text: Optional[str] = None,
    creator_id: str = "creator",
    reroll_count: int = 0,
) -> dict[str, Any]:
    add_trace_metadata(
        tool_name="design_creator_voice",
        role="creator",
        stage="voice_candidates",
        run_id=ctx.run_id,
    )
    from orchestrator.creative_contracts import CreatorVoiceSpec

    spec_obj = CreatorVoiceSpec.model_validate(spec)
    description_hash = voice_description_hash(spec_obj)

    async def design() -> dict[str, Any]:
        batch = await ctx.adapter.design_voice_candidates(
            spec_obj,
            preview_text=preview_text,
        )
        return require_dict(
            batch.model_dump() if hasattr(batch, "model_dump") else batch,
            tool_name="design_creator_voice_tool",
        )

    if not direct_elevenlabs_voice_enabled(ctx):
        return await design()
    sample = (preview_text or DEFAULT_PREVIEW_TEXT).strip()
    return await execute_paid_effect(
        ctx,
        effect_key=(
            f"voice-design:{ctx.run_id}:{creator_id}:"
            f"{description_hash}:{reroll_count}"
        ),
        provider="elevenlabs_voice_design_chars",
        units=len(sample),
        request={
            "creator_id": creator_id,
            "description_hash": description_hash,
            "model": str(ctx.pipeline.get("voice", {}).get("design_model") or ""),
            "preview_characters": len(sample),
            "reroll": reroll_count,
        },
        operation=design,
    )


@traced(
    "tool.finalize_creator_voice",
    run_type="tool",
    tool_name="finalize_creator_voice",
    role="creator",
    stage="finalize_voices",
)
async def finalize_creator_voice_tool(
    ctx: ToolContext,
    *,
    candidate_id: str,
    batch: dict[str, Any],
    creator_id: str,
    organization_id: str = "default",
) -> dict[str, Any]:
    add_trace_metadata(
        tool_name="finalize_creator_voice",
        role="creator",
        stage="finalize_voices",
        run_id=ctx.run_id,
    )
    async def finalize() -> dict[str, Any]:
        finalized = await ctx.adapter.finalize_voice(
            candidate_id,
            batch=batch,
            creator_id=creator_id,
            organization_id=organization_id,
        )
        return require_dict(
            finalized.model_dump() if hasattr(finalized, "model_dump") else finalized,
            tool_name="finalize_creator_voice_tool",
        )

    if not direct_elevenlabs_voice_enabled(ctx):
        return await finalize()

    async def reconcile() -> dict[str, Any]:
        reconciled = await ctx.adapter.reconcile_voice(
            candidate_id,
            batch=batch,
            creator_id=creator_id,
            organization_id=organization_id,
        )
        return require_dict(
            reconciled.model_dump()
            if hasattr(reconciled, "model_dump")
            else reconciled,
            tool_name="finalize_creator_voice_tool",
        )

    return await execute_paid_effect(
        ctx,
        effect_key=f"voice-finalize:{ctx.run_id}:{creator_id}:{candidate_id}",
        provider="elevenlabs_voice_slots",
        units=1,
        request={
            "creator_id": creator_id,
            "candidate_id": candidate_id,
            "description_hash": str(batch.get("description_hash") or ""),
        },
        operation=finalize,
        reconcile=reconcile,
    )
