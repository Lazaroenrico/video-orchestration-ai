"""Script-writing tools."""
from __future__ import annotations

from typing import Any, Optional

from orchestrator.creative_contracts import (
    ScriptResult,
    ScriptSubmission,
    SpokenBeat,
    materialize_script,
)
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
    revision: Optional[str] = None,
    persona: Optional[str] = None,
    campaign: Optional[dict[str, Any]] = None,
    draft: Optional[dict[str, Any]] = None,
    revision_feedback: Optional[str] = None,
    return_contract: bool = False,
    agent_submission: bool = False,
    target_duration_seconds: Optional[int] = None,
    min_spoken_words: Optional[int] = None,
    max_spoken_words: Optional[int] = None,
) -> str | ScriptResult:
    add_trace_metadata(
        tool_name="write_script",
        role="llm",
        stage="scripts",
        run_id=ctx.run_id,
    )
    if draft is None and agent_submission:
        raise ValueError("agent must submit a structured draft")
    if draft is None:
        kwargs: dict[str, Any] = {
            "concept": concept,
            "creator_ref": creator_ref,
            "platform": platform,
            "revision": revision_feedback or revision,
        }
        if persona is not None:
            kwargs["persona"] = persona
        script = require_non_empty_string(
            await ctx.adapter.write_script(**kwargs),
            tool_name="write_script_tool",
        )
        submission = _submission_from_text(script, concept)
    else:
        submission = ScriptSubmission.model_validate(draft)
        script = ""

    spoken_seconds = sum(beat.seconds for beat in submission.spoken_beats)
    if (
        target_duration_seconds is not None
        and max(spoken_seconds, submission.estimated_duration)
        > target_duration_seconds
    ):
        raise ValueError(
            f"narration exceeds {target_duration_seconds} seconds"
        )
    spoken_words = sum(
        len(beat.text.split()) for beat in submission.spoken_beats
    )
    if min_spoken_words is not None and spoken_words < min_spoken_words:
        raise ValueError(
            f"narration requires at least {min_spoken_words} spoken words (got {spoken_words})"
        )
    if max_spoken_words is not None and spoken_words > max_spoken_words:
        raise ValueError(
            f"narration exceeds {max_spoken_words} spoken words (got {spoken_words})"
        )

    concept_id = str(concept.get("id") or "")
    if not concept_id:
        raise ValueError("concept id is required")
    materialized = materialize_script(
        submission,
        run_id=ctx.run_id,
        concept_id=concept_id,
    )
    rendered = materialized.render_text() if draft is not None else script
    result = ScriptResult(script=rendered, script_draft=materialized)
    return result if return_contract else result.script


def _submission_from_text(
    script: str,
    concept: dict[str, Any],
) -> ScriptSubmission:
    sections: list[SpokenBeat] = []
    for raw_line in script.splitlines():
        label, separator, text = raw_line.partition(":")
        section = label.strip().lower()
        if separator and section in {"hook", "body", "cta"} and text.strip():
            spoken = text.strip()
            sections.append(
                SpokenBeat(
                    section=section,
                    text=spoken,
                    seconds=max(1, round(len(spoken.split()) / 2.5)),
                )
            )
    if not sections:
        sections = [SpokenBeat(section="body", text=script, seconds=10)]
    cta = next(
        (beat.text for beat in sections if beat.section == "cta"),
        "See the offer",
    )
    return ScriptSubmission(
        spoken_beats=sections,
        visual_beats=[
            f"Creator performs a {concept.get('format') or 'talking_head'} scene"
        ],
        on_screen_text=[str(concept.get("hook") or "")] if concept.get("hook") else [],
        call_to_action=cta,
        estimated_duration=sum(beat.seconds for beat in sections),
    )
