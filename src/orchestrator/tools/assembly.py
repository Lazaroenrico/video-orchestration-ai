"""Assembly and final-upscale tools."""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from orchestrator.adapters.base import RenderedMedia
from orchestrator.graph.state import Artifact, Item
from orchestrator.tools.base import (
    ToolContext,
    direct_elevenlabs_voice_enabled,
    execute_paid_effect,
    require_artifact,
    require_non_empty_string,
)
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.assemble_video",
    run_type="tool",
    tool_name="assemble_video",
    role="assembly",
    stage="assembly",
)
async def assemble_video_tool(
    ctx: ToolContext,
    *,
    item: Item,
    platform: str,
    system_prompt: Optional[str] = None,
) -> Artifact | RenderedMedia:
    add_trace_metadata(
        tool_name="assemble_video",
        role="assembly",
        stage="assembly",
        run_id=ctx.run_id,
        item_id=item.id,
    )
    art = await ctx.adapter.assemble(
        item=item, platform=platform, system_prompt=system_prompt,
    )
    if isinstance(art, RenderedMedia):
        if not art.data or not art.content_type:
            raise ValueError("assemble_video_tool received empty rendered media")
        return art
    return require_artifact(art, tool_name="assemble_video_tool")


@traced(
    "tool.synthesize_voiceover",
    run_type="tool",
    tool_name="synthesize_voiceover",
    role="creator",
    stage="voiceover",
)
async def synthesize_voiceover_tool(
    ctx: ToolContext,
    *,
    voice_ref: str,
    text: str,
    item_id: str,
) -> Artifact:
    add_trace_metadata(
        tool_name="synthesize_voiceover",
        role="creator",
        stage="voiceover",
        run_id=ctx.run_id,
    )
    async def synthesize() -> dict[str, Any]:
        art = await ctx.adapter.synthesize_voiceover(
            voice_ref=voice_ref,
            text=text,
        )
        validated = require_artifact(art, tool_name="synthesize_voiceover_tool")
        return validated.model_dump(mode="json")

    if direct_elevenlabs_voice_enabled(ctx):
        script_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        result = await execute_paid_effect(
            ctx,
            effect_key=(
                f"voiceover:{ctx.run_id}:{item_id}:{script_hash}:{voice_ref}"
            ),
            provider="elevenlabs_tts_chars",
            units=len(text),
            request={
                "item_id": item_id,
                "script_hash": script_hash,
                "voice_ref": voice_ref,
                "characters": len(text),
                "model": str(ctx.pipeline.get("voice", {}).get("tts_model") or ""),
            },
            operation=synthesize,
        )
    else:
        result = await synthesize()
    return require_artifact(result, tool_name="synthesize_voiceover_tool")


@traced(
    "tool.upscale_video",
    run_type="tool",
    tool_name="upscale_video",
    role="upscale",
    stage="upscale",
)
async def upscale_video_tool(ctx: ToolContext, *, media_uri: str) -> str:
    add_trace_metadata(
        tool_name="upscale_video",
        role="upscale",
        stage="upscale",
        run_id=ctx.run_id,
    )
    upscaled_uri = await ctx.adapter.upscale(media_uri)
    return require_non_empty_string(upscaled_uri, tool_name="upscale_video_tool")
