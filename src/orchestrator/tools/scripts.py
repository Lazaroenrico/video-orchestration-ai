"""Script-writing tools."""
from __future__ import annotations

from typing import Any

from orchestrator.tools.base import ToolContext, require_non_empty_string
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.write_script",
    run_type="tool",
    tool_name="write_script",
    role="llm",
    stage="scripts",
)
async def write_script_tool(
    ctx: ToolContext,
    *,
    concept: dict[str, Any],
    creator_ref: str,
    platform: str,
) -> str:
    add_trace_metadata(
        tool_name="write_script",
        role="llm",
        stage="scripts",
        run_id=ctx.run_id,
    )
    script = await ctx.adapter.write_script(
        concept=concept, creator_ref=creator_ref, platform=platform,
    )
    return require_non_empty_string(script, tool_name="write_script_tool")
