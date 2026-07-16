"""Video generation tools."""
from __future__ import annotations

from typing import Optional

from orchestrator.graph.state import Artifact
from orchestrator.tools.base import ToolContext, require_artifact
from orchestrator.tracing import add_trace_metadata, traced

# Precedência explícita: a tool não conhece o conteúdo dos guardrails (montados por
# ``_video_prompt`` nos nodes), então declara que o brief acima manda em vez de tentar
# re-injetá-los. O agent refina a take; nunca revoga "No mock footage" (D33).
_REVISION_TEMPLATE = (
    "Revision directive (refine the take within the brief above; "
    "the brief and its constraints above always win):\n{revision}"
)


def _compose_prompt(system_prompt: Optional[str], revision: Optional[str]) -> Optional[str]:
    """Apenda a diretiva do agent ao brief server-authored, preservando-o intacto."""
    directive = (revision or "").strip()
    if not directive:
        return system_prompt
    block = _REVISION_TEMPLATE.format(revision=directive)
    return f"{system_prompt}\n\n{block}" if system_prompt else block


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
    revision: Optional[str] = None,
    stage: str = "video",
) -> Artifact:
    """Gera um clip. ``revision`` é a única alavanca do agent (D33): uma diretiva
    apendada ao brief; todo o resto é server-authoritative.
    """
    add_trace_metadata(
        tool_name="generate_clip",
        role="video",
        stage=stage,
        run_id=ctx.run_id,
        item_id=item_id,
        tier=tier,
        revision=(revision or "").strip() or None,
    )
    clip = await ctx.adapter.generate_clip(
        item_id=item_id,
        tier=tier,
        seconds=seconds,
        attempt=attempt,
        system_prompt=_compose_prompt(system_prompt, revision),
        reference_image_uri=reference_image_uri,
    )
    return require_artifact(clip, tool_name="generate_clip_tool")
