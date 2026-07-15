"""Declarative stage/tool catalog for future agent execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.tools.registry import TOOL_REGISTRY, get_tool_spec, tool_specs_for_stage


_EXECUTORS = {"tool", "agent"}
_AGENT_STAGES = {"concepts", "scripts"}


def is_agent_stage_allowed(stage: str) -> bool:
    """Fonte única da verdade: quais stages podem rodar em modo agent.

    Usada tanto no load do catálogo (`build_agent_catalog`) quanto em runtime pelo
    stage executor, para o invariante não morar só no loader do YAML.
    """
    return stage in _AGENT_STAGES


def agent_stage_not_allowed_message() -> str:
    allowed = ", ".join(sorted(_AGENT_STAGES))
    return f"agent execution is only supported for stages: {allowed}"


@dataclass(frozen=True)
class StageExecutionSpec:
    stage: str
    executor: str
    tools: tuple[str, ...]
    target_model: str | None = None
    target_agent: str | None = None
    agent_enabled: bool = False


@dataclass(frozen=True)
class AgentCatalog:
    stages: tuple[StageExecutionSpec, ...]

    def stage(self, name: str) -> StageExecutionSpec:
        for spec in self.stages:
            if spec.stage == name:
                return spec
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stages": {
                spec.stage: {
                    "executor": spec.executor,
                    "tools": list(spec.tools),
                    "target_model": spec.target_model,
                    "target_agent": spec.target_agent,
                    "agent_enabled": spec.agent_enabled,
                }
                for spec in self.stages
            }
        }


def default_agent_catalog() -> AgentCatalog:
    stages = sorted({spec.stage for spec in TOOL_REGISTRY})
    specs = tuple(
        StageExecutionSpec(
            stage=stage,
            executor="tool",
            tools=tuple(spec.name for spec in tool_specs_for_stage(stage)),
        )
        for stage in stages
    )
    return AgentCatalog(stages=specs)


def build_agent_catalog(raw: dict[str, Any] | None = None) -> AgentCatalog:
    catalog = default_agent_catalog()
    data = raw or {}
    stages_raw = data.get("stages", {})
    if stages_raw is None:
        stages_raw = {}
    if not isinstance(stages_raw, dict):
        raise ValueError("agents.yaml: stages must be a mapping")

    by_stage = {spec.stage: spec for spec in catalog.stages}
    for stage, override in stages_raw.items():
        stage_name = str(stage)
        if stage_name not in by_stage:
            raise ValueError(f"agents.yaml: unknown stage {stage_name!r}")
        if not isinstance(override, dict):
            raise ValueError(f"agents.yaml: stage {stage_name!r} must be a mapping")

        base = by_stage[stage_name]
        executor = str(override.get("executor", base.executor))
        if executor not in _EXECUTORS:
            raise ValueError(f"agents.yaml: stage {stage_name!r} has invalid executor {executor!r}")

        raw_tools = override.get("tools", base.tools)
        if not isinstance(raw_tools, list | tuple) or not raw_tools:
            raise ValueError(f"agents.yaml: stage {stage_name!r} tools must be a non-empty list")
        tools = tuple(str(tool) for tool in raw_tools)
        for tool in tools:
            try:
                tool_spec = get_tool_spec(tool)
            except KeyError as exc:
                raise ValueError(f"agents.yaml: unknown tool {tool!r}") from exc
            if tool_spec.stage != stage_name:
                raise ValueError(
                    f"agents.yaml: tool {tool!r} belongs to stage {tool_spec.stage!r}, "
                    f"not {stage_name!r}"
                )

        agent_enabled = bool(override.get("agent_enabled", base.agent_enabled))
        if executor == "agent" and not agent_enabled:
            raise ValueError(
                f"agents.yaml: stage {stage_name!r} executor: agent "
                "requires agent_enabled: true"
            )
        if agent_enabled and executor != "agent":
            raise ValueError(
                f"agents.yaml: stage {stage_name!r} agent_enabled: true "
                "requires executor: agent"
            )
        if executor == "agent" and not is_agent_stage_allowed(stage_name):
            raise ValueError(f"agents.yaml: {agent_stage_not_allowed_message()}")

        by_stage[stage_name] = StageExecutionSpec(
            stage=stage_name,
            executor=executor,
            tools=tools,
            target_model=override.get("target_model", base.target_model),
            target_agent=override.get("target_agent", base.target_agent),
            agent_enabled=agent_enabled,
        )

    return AgentCatalog(stages=tuple(by_stage[spec.stage] for spec in catalog.stages))
