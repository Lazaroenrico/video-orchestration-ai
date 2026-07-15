"""Static tool metadata for future agent routing."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    role: str
    stage: str
    function_path: str = ""
    target_model: str | None = None
    target_agent: str | None = None
    agent_enabled: bool = False
    capabilities: tuple[str, ...] = ()


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="generate_concepts",
        description="Generate a batch of UGC concepts for an offer.",
        role="llm",
        stage="concepts",
        function_path="orchestrator.tools.concepts.generate_concepts_tool",
        capabilities=("llm", "batch_generation", "concept_generation"),
    ),
    ToolSpec(
        name="write_script",
        description="Write a platform-calibrated script for one concept.",
        role="llm",
        stage="scripts",
        function_path="orchestrator.tools.scripts.write_script_tool",
        capabilities=("llm", "copywriting", "script_generation"),
    ),
    ToolSpec(
        name="build_creator",
        description="Build one reusable creator identity with image and voice metadata.",
        role="creator",
        stage="roster",
        function_path="orchestrator.tools.creators.build_creator_tool",
        capabilities=("creator_identity", "image_generation", "voice_generation"),
    ),
    ToolSpec(
        name="generate_clip",
        description="Generate a silent video clip for an item and tier.",
        role="video",
        stage="video",
        function_path="orchestrator.tools.video.generate_clip_tool",
        capabilities=("video_generation", "artifact_generation"),
    ),
    ToolSpec(
        name="qc_check",
        description="Evaluate an item and return a structured QC result.",
        role="qc",
        stage="qc",
        function_path="orchestrator.tools.qc.qc_check_tool",
        capabilities=("quality_control", "structured_evaluation"),
    ),
    ToolSpec(
        name="assemble_video",
        description="Assemble approved item material into the final video artifact.",
        role="assembly",
        stage="assembly",
        function_path="orchestrator.tools.assembly.assemble_video_tool",
        capabilities=("video_assembly", "artifact_generation"),
    ),
    ToolSpec(
        name="upscale_video",
        description="Upscale the final assembled video URI.",
        role="upscale",
        stage="upscale",
        function_path="orchestrator.tools.assembly.upscale_video_tool",
        capabilities=("video_upscale", "artifact_enhancement"),
    ),
)


def get_tool_spec(name: str) -> ToolSpec:
    for spec in TOOL_REGISTRY:
        if spec.name == name:
            return spec
    raise KeyError(name)


def tool_specs_for_stage(stage: str) -> tuple[ToolSpec, ...]:
    return tuple(spec for spec in TOOL_REGISTRY if spec.stage == stage)


def resolve_tool_function(spec: ToolSpec) -> Any:
    if not spec.function_path:
        raise ValueError(f"{spec.name} does not declare function_path")
    module_name, function_name = spec.function_path.rsplit(".", 1)
    module = import_module(module_name)
    return getattr(module, function_name)
