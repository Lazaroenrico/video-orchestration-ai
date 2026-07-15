"""Video generation tools."""
from __future__ import annotations

from typing import Optional

from orchestrator.graph.state import Artifact
from orchestrator.tools.base import ToolContext, require_artifact
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.generate_clip",
    run_type="tool",
    tool_name="generate_clip",
    role="video",
    stage="video",
)
async def generate_clip_tool(
    ctx: ToolContext,
    *,
    item_id: str,
    tier: str,
    seconds: int,
    attempt: int,
    system_prompt: Optional[str] = None,
    reference_image_uri: Optional[str] = None,
    stage: str = "video",
) -> Artifact:
    add_trace_metadata(
        tool_name="generate_clip",
        role="video",
        stage=stage,
        run_id=ctx.run_id,
        item_id=item_id,
        tier=tier,
    )
    clip = await ctx.adapter.generate_clip(
        item_id=item_id,
        tier=tier,
        seconds=seconds,
        attempt=attempt,
        system_prompt=system_prompt,
        reference_image_uri=reference_image_uri,
    )
    return require_artifact(clip, tool_name="generate_clip_tool")
