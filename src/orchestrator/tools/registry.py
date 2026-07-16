"""Static tool metadata for future agent routing."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any


# Schema agent-facing dos stages LLM-only: a única alavanca do modelo é ``revision``
# (uma diretiva de refino). offer/n/seed (concepts) e concept/creator_ref/platform
# (scripts) ficam server-authoritative — injetados pelo run_tool no stage_executor,
# nunca controlados pelo modelo. ``revision`` opcional: sem ela = draft inicial.
_REVISION_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "revision": {
            "type": "string",
            "description": (
                "Optional one-line revision directive to improve the previous draft. "
                "Omit on the first call to produce the initial draft."
            ),
        }
    },
    "required": [],
    "additionalProperties": False,
}

_EMPTY_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# Schema agent-facing do stage video (D33). Mesma alavanca única dos stages de texto —
# ``revision`` —, reusando o nome que os brains já ensinam no system prompt. A diretiva é
# apendada ao brief server-authored (que sempre vence). Fora do schema, e portanto
# impossíveis de o modelo tocar: ``tier`` (vem do tier routing e define o custo — seedance
# é ~17x ltx), ``attempt`` (vem do loop de QC), ``item_id`` (identidade), ``seconds``,
# ``system_prompt`` e ``reference_image_uri``.
_VIDEO_REVISION_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "revision": {
            "type": "string",
            "description": (
                "Optional one-line directive appended to the shot brief to improve the "
                "previous take (e.g. framing, pacing, energy). The existing brief and "
                "its constraints always win. Omit on the first call to produce the base take."
            ),
        }
    },
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
    # JSON schema dos params que o agent pode controlar ao chamar a tool (Fase 1).
    parameters: dict[str, Any] = field(default_factory=lambda: dict(_EMPTY_PARAM_SCHEMA))


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="generate_concepts",
        description="Generate a batch of UGC concepts for an offer.",
        role="llm",
        stage="concepts",
        function_path="orchestrator.tools.concepts.generate_concepts_tool",
        capabilities=("llm", "batch_generation", "concept_generation"),
        parameters=dict(_REVISION_PARAM_SCHEMA),
    ),
    ToolSpec(
        name="write_script",
        description="Write a platform-calibrated script for one concept.",
        role="llm",
        stage="scripts",
        function_path="orchestrator.tools.scripts.write_script_tool",
        capabilities=("llm", "copywriting", "script_generation"),
        parameters=dict(_REVISION_PARAM_SCHEMA),
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
        parameters=dict(_VIDEO_REVISION_PARAM_SCHEMA),
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
