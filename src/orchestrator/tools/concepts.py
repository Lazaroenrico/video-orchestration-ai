"""Concept-generation tools."""
from __future__ import annotations

from typing import Any, Optional

from orchestrator.tools.base import ToolContext, require_dict_list
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.generate_concepts",
    run_type="tool",
    tool_name="generate_concepts",
    role="llm",
    stage="concepts",
)
async def generate_concepts_tool(
    ctx: ToolContext,
    *,
    offer: str,
    n: int,
    seed: str,
    bias: Optional[list[str]] = None,
    revision: Optional[str] = None,
    persona: Optional[str] = None,
) -> list[dict[str, Any]]:
    add_trace_metadata(
        tool_name="generate_concepts",
        role="llm",
        stage="concepts",
        run_id=ctx.run_id,
    )
    kwargs: dict[str, Any] = {
        "offer": offer,
        "n": n,
        "seed": seed,
        "bias": bias,
        "revision": revision,
    }
    if persona is not None:
        kwargs["persona"] = persona
    concepts = await ctx.adapter.generate_concepts(**kwargs)
    return require_dict_list(concepts, tool_name="generate_concepts_tool")
