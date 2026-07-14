"""Static tool metadata for future agent routing."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    role: str
    stage: str


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="generate_concepts",
        description="Generate a batch of UGC concepts for an offer.",
        role="llm",
        stage="concepts",
    ),
    ToolSpec(
        name="write_script",
        description="Write a platform-calibrated script for one concept.",
        role="llm",
        stage="scripts",
    ),
    ToolSpec(
        name="build_creator",
        description="Build one reusable creator identity with image and voice metadata.",
        role="creator",
        stage="roster",
    ),
    ToolSpec(
        name="generate_clip",
        description="Generate a silent video clip for an item and tier.",
        role="video",
        stage="video",
    ),
    ToolSpec(
        name="qc_check",
        description="Evaluate an item and return a structured QC result.",
        role="qc",
        stage="qc",
    ),
    ToolSpec(
        name="assemble_video",
        description="Assemble approved item material into the final video artifact.",
        role="assembly",
        stage="assembly",
    ),
    ToolSpec(
        name="upscale_video",
        description="Upscale the final assembled video URI.",
        role="upscale",
        stage="upscale",
    ),
)
