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
