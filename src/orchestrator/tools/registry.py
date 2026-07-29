"""Static tool metadata for future agent routing."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any


_CONCEPT_SUBMISSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hook": {"type": "string"},
                    "angle": {"type": "string"},
                    "audience_problem": {"type": "string"},
                    "product_mechanism": {"type": "string"},
                    "evidence_basis": {
                        "type": "string",
                        "enum": ["provided_fact", "performance", "cold_test"],
                    },
                    "format": {"type": "string"},
                    "hook_style": {"type": "string"},
                },
                "required": [
                    "hook",
                    "angle",
                    "audience_problem",
                    "product_mechanism",
                    "evidence_basis",
                    "format",
                    "hook_style",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["proposals"],
    "additionalProperties": False,
}

_SCRIPT_SUBMISSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "draft": {
            "type": "object",
            "properties": {
                "spoken_beats": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {
                                "type": "string",
                                "enum": ["hook", "body", "cta"],
                            },
                            "text": {"type": "string"},
                            "seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                        },
                        "required": ["section", "text", "seconds"],
                        "additionalProperties": False,
                    },
                },
                "visual_beats": {"type": "array", "items": {"type": "string"}},
                "on_screen_text": {"type": "array", "items": {"type": "string"}},
                "call_to_action": {"type": "string"},
                "estimated_duration": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 180,
                },
            },
            "required": [
                "spoken_beats",
                "visual_beats",
                "on_screen_text",
                "call_to_action",
                "estimated_duration",
            ],
            "additionalProperties": False,
        },
    },
    "required": ["draft"],
    "additionalProperties": False,
}

_CREATOR_ROSTER_SUBMISSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "creators": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "archetype": {"type": "string"},
                    "visual_brief": {"type": "string"},
                    "voice_brief": {"type": "string"},
                    "performance_style": {"type": "string"},
                    "exclusions": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "archetype",
                    "visual_brief",
                    "voice_brief",
                    "performance_style",
                    "exclusions",
                ],
                "additionalProperties": False,
            },
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "string"},
                    "creator_index": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": ["concept_id", "creator_index"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["creators", "assignments"],
    "additionalProperties": False,
}

_EMPTY_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

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
    terminal_submission: bool = False
    # JSON schema dos params que o agent pode controlar ao chamar a tool (Fase 1).
    parameters: dict[str, Any] = field(default_factory=lambda: dict(_EMPTY_PARAM_SCHEMA))


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="write_persona",
        description="Write the batch-level creator persona for an offer.",
        role="llm",
        stage="persona",
        function_path="orchestrator.tools.persona.write_persona_tool",
        capabilities=("llm", "persona_generation", "brand_context"),
        parameters=dict(_EMPTY_PARAM_SCHEMA),
    ),
    ToolSpec(
        name="generate_concepts",
        description="Generate a batch of UGC concepts for an offer.",
        role="llm",
        stage="concepts",
        function_path="orchestrator.tools.concepts.generate_concepts_tool",
        capabilities=("llm", "batch_generation", "concept_generation"),
        parameters=dict(_CONCEPT_SUBMISSION_SCHEMA),
        terminal_submission=True,
    ),
    ToolSpec(
        name="write_script",
        description="Write a platform-calibrated script for one concept.",
        role="llm",
        stage="scripts",
        function_path="orchestrator.tools.scripts.write_script_tool",
        capabilities=("llm", "copywriting", "script_generation"),
        parameters=dict(_SCRIPT_SUBMISSION_SCHEMA),
        terminal_submission=True,
    ),
    ToolSpec(
        name="design_creator_roster",
        description="Submit exactly two creator profiles and assign every concept.",
        role="llm",
        stage="creator_profiles",
        function_path="orchestrator.tools.creator_profiles.design_creator_roster_tool",
        capabilities=("llm", "creator_strategy", "casting"),
        parameters=dict(_CREATOR_ROSTER_SUBMISSION_SCHEMA),
        terminal_submission=True,
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
        parameters=dict(_EMPTY_PARAM_SCHEMA),
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


def tool_call_schemas(names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Contrato neutro (name/description/parameters) das tools que o agent pode chamar.

    Os adapters formatam isso para o provider: OpenAI function-calling
    (``{"type":"function","function":{...}}``) ou Anthropic (``input_schema``).
    ``KeyError`` para nomes não registrados — só tools do registry viram schema.
    """
    schemas: list[dict[str, Any]] = []
    for name in names:
        spec = get_tool_spec(name)
        schemas.append(
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": dict(spec.parameters),
            }
        )
    return schemas


def resolve_tool_function(spec: ToolSpec) -> Any:
    if not spec.function_path:
        raise ValueError(f"{spec.name} does not declare function_path")
    module_name, function_name = spec.function_path.rsplit(".", 1)
    module = import_module(module_name)
    return getattr(module, function_name)
