"""Persona-writing tools."""
from __future__ import annotations

from typing import Optional

from orchestrator.tools.base import ToolContext, require_non_empty_string
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.write_persona",
    run_type="tool",
    tool_name="write_persona",
    role="llm",
    stage="persona",
)
async def write_persona_tool(
    ctx: ToolContext,
    *,
    offer: str,
    brief: Optional[str] = None,
    revision: Optional[str] = None,
) -> str:
    add_trace_metadata(
        tool_name="write_persona",
        role="llm",
        stage="persona",
        run_id=ctx.run_id,
    )
    persona = await ctx.adapter.write_persona(
        offer=offer,
        brief=brief,
        revision=revision,
    )
    return require_non_empty_string(persona, tool_name="write_persona_tool")
