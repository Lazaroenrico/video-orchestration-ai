"""Assembly and final-upscale tools."""
from __future__ import annotations

from typing import Optional

from orchestrator.graph.state import Artifact, Item
from orchestrator.tools.base import (
    ToolContext,
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
) -> Artifact:
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
    return require_artifact(art, tool_name="assemble_video_tool")


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
