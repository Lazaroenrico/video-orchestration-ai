"""Creator-building tools."""
from __future__ import annotations

from typing import Any, Optional

from orchestrator.adapters.base import VoiceProfile
from orchestrator.tools.base import ToolContext, require_dict
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
) -> dict[str, Any]:
    add_trace_metadata(
        tool_name="build_creator",
        role="creator",
        stage="roster",
        run_id=ctx.run_id,
    )
    creator = await ctx.adapter.build_creator(
        index=index, system_prompt=system_prompt, voice_profile=voice_profile,
    )
    return require_dict(creator, tool_name="build_creator_tool")


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
    from orchestrator.creative_contracts import CreatorVoiceSpec

    voice_brief = (profile.get("voice_brief") or "").casefold()
    perf_style = (profile.get("performance_style") or "").casefold()
    archetype = (profile.get("archetype") or "").casefold()

    if any(k in voice_brief or k in archetype for k in ("female", "woman", "mulher", "feminina")):
        vocal_pres = "feminine"
    elif any(k in voice_brief or k in archetype for k in ("male", "man", "homem", "masculino")):
        vocal_pres = "masculine"
    else:
        vocal_pres = "neutral"

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
) -> dict[str, Any]:
    add_trace_metadata(
        tool_name="design_creator_voice",
        role="creator",
        stage="voice_candidates",
        run_id=ctx.run_id,
    )
    from orchestrator.creative_contracts import CreatorVoiceSpec

    spec_obj = CreatorVoiceSpec.model_validate(spec)
    batch = await ctx.adapter.design_voice_candidates(
        spec_obj, preview_text=preview_text
    )
    return require_dict(
        batch.model_dump() if hasattr(batch, "model_dump") else batch,
        tool_name="design_creator_voice_tool",
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
