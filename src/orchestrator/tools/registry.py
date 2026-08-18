"""Static tool metadata for future agent routing."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    role: str
    stage: str
    terminal_submission: bool = False


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="generate_concepts",
        description="Generate a batch of UGC concepts for an offer.",
        role="llm",
        stage="concepts",
        terminal_submission=True,
    ),
    ToolSpec(
        name="write_script",
        description="Write a platform-calibrated script for one concept.",
        role="llm",
        stage="scripts",
        terminal_submission=True,
    ),
    ToolSpec(
        name="design_creator_roster",
        description="Submit exactly two creator profiles and assign every concept.",
        role="llm",
        stage="creator_profiles",
        terminal_submission=True,
    ),
)


def get_tool_spec(name: str) -> ToolSpec:
    for spec in TOOL_REGISTRY:
        if spec.name == name:
            return spec
    raise KeyError(name)


def tool_specs_for_stage(stage: str) -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in TOOL_REGISTRY if spec.stage == stage)
