"""Quality-control tools."""
from __future__ import annotations

from orchestrator.graph.state import Item, QCResult
from orchestrator.tools.base import ToolContext, require_qc_result
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.qc_check",
    run_type="tool",
    tool_name="qc_check",
    role="qc",
    stage="qc",
)
async def qc_check_tool(ctx: ToolContext, *, item: Item, fail_rate: float) -> QCResult:
    add_trace_metadata(
        tool_name="qc_check",
        role="qc",
        stage="qc",
        run_id=ctx.run_id,
        item_id=item.id,
    )
    qc = await ctx.adapter.qc_check(item=item, fail_rate=fail_rate)
    return require_qc_result(qc, tool_name="qc_check_tool")
