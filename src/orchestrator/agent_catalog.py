from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.tools.registry import TOOL_REGISTRY, get_tool_spec, tool_specs_for_stage

_EXECUTORS = {"tool", "agent"}
# Stages que podem rodar em modo agent. ``video`` entrou no D33 (agent escolhe a diretiva
# de refino da take; tier/attempt seguem server-authoritative). roster/assembly/upscale
# continuam fora até terem contrato de artefato testado.
_AGENT_STAGES = {"concepts", "scripts", "creator_profiles"}


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
    system_prompt_path: str | None = None
    system_prompt: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    schema_version: str | None = None
    agent_enabled: bool = False
    allowed_models: tuple[str, ...] = ()


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
                    "has_system_prompt": bool(spec.system_prompt and spec.system_prompt.strip()),
                    "prompt_version": spec.prompt_version,
                    "prompt_hash": spec.prompt_hash,
                    "schema_version": spec.schema_version,
                    "agent_enabled": spec.agent_enabled,
                    "allowed_models": list(spec.allowed_models),
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


def _first_existing(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.exists()), None)


def _load_system_prompt(
    base_dir: Path,
    rel_path: str | None,
    fallback_dirs: tuple[Path, ...] = (),
) -> tuple[str | None, str | None]:
    if rel_path is None:
        return None, None

    prompt_path = Path(rel_path)
    if prompt_path.is_absolute() or ".." in prompt_path.parts:
        raise ValueError(f"agents.yaml: invalid system_prompt_path {rel_path!r}")

    # Perfil primeiro; sem override, cai na base compartilhada (config-base).
    full_path = _first_existing(
        [base_dir / prompt_path, *(fb / prompt_path for fb in fallback_dirs)]
    )
    if full_path is None:
        raise ValueError(f"agents.yaml: system_prompt_path not found: {rel_path}")

    stage_prompt = full_path.read_text(encoding="utf-8").strip()
    if not stage_prompt:
        raise ValueError(f"agents.yaml: empty system prompt at {rel_path}")

    shared_rel = Path("prompts") / "agents" / "_shared.md"
    shared_path = _first_existing(
        [base_dir / shared_rel, *(fb / shared_rel for fb in fallback_dirs)]
    )
    if shared_path is not None:
        shared_prompt = shared_path.read_text(encoding="utf-8").strip()
        if shared_prompt:
            return rel_path, f"{shared_prompt}\n\n{stage_prompt}"
    return rel_path, stage_prompt


def build_agent_catalog(
    raw: dict[str, Any] | None = None,
    *,
    base_dir: str | Path | None = None,
    fallback_dirs: tuple[Path, ...] = (),
) -> AgentCatalog:
    catalog = default_agent_catalog()
    data = raw or {}
    prompt_base = Path(base_dir or ".")
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
                f"agents.yaml: stage {stage_name!r} executor: agent requires agent_enabled: true"
            )
        if agent_enabled and executor != "agent":
            raise ValueError(
                f"agents.yaml: stage {stage_name!r} agent_enabled: true requires executor: agent"
            )
        if executor == "agent" and not is_agent_stage_allowed(stage_name):
            raise ValueError(f"agents.yaml: {agent_stage_not_allowed_message()}")

        raw_allowed = override.get("allowed_models")
        if raw_allowed is None:
            allowed_models = base.allowed_models
        elif isinstance(raw_allowed, list | tuple):
            if not all(isinstance(m, str) and m.strip() for m in raw_allowed):
                raise ValueError(
                    f"agents.yaml: stage {stage_name!r} allowed_models must contain non-empty strings"
                )
            allowed_models = tuple(str(m).strip() for m in raw_allowed)
        else:
            raise ValueError(
                f"agents.yaml: stage {stage_name!r} allowed_models must be a list of strings"
            )

        system_prompt_path, system_prompt = _load_system_prompt(
            prompt_base,
            override.get("system_prompt_path", base.system_prompt_path),
            fallback_dirs=fallback_dirs,
        )
        prompt_hash = (
            hashlib.sha256(system_prompt.encode()).hexdigest()
            if system_prompt is not None
            else None
        )

        by_stage[stage_name] = StageExecutionSpec(
            stage=stage_name,
            executor=executor,
            tools=tools,
            target_model=override.get("target_model", base.target_model),
            target_agent=override.get("target_agent", base.target_agent),
            system_prompt_path=system_prompt_path,
            system_prompt=system_prompt,
            prompt_version=override.get("prompt_version"),
            prompt_hash=prompt_hash,
            schema_version=override.get("schema_version"),
            agent_enabled=agent_enabled,
            allowed_models=allowed_models,
        )

    return AgentCatalog(stages=tuple(by_stage[spec.stage] for spec in catalog.stages))


def with_stage_model(catalog: AgentCatalog, stage: str, model: str | None) -> AgentCatalog:
    """Retorna uma nova instância do AgentCatalog com target_model alterado para o stage.

    Se `model` for None ou vazio, retorna o catálogo inalterado.
    A validação da whitelist é estritamente fail-closed: `model` só é aceito se estiver
    explicitamente presente em `spec.allowed_models`. Se `allowed_models` for vazio ou
    não contiver o modelo solicitado, levanta `ValueError`.
    """
    if model is None or not str(model).strip():
        return catalog
    model_str = str(model).strip()
    spec = catalog.stage(stage)
    if model_str not in spec.allowed_models:
        raise ValueError(
            f"model {model_str!r} is not allowed for stage {stage!r}; allowed models: {list(spec.allowed_models)}"
        )
    import dataclasses

    new_spec = dataclasses.replace(spec, target_model=model_str)
    new_stages = tuple(new_spec if s.stage == stage else s for s in catalog.stages)
    return AgentCatalog(stages=new_stages)


def extract_script_model(*sources: Any, script_model: str | None = None) -> str | None:
    """Extrai o identificador de script_model respeitando a precedência canônica.

    Precedência:
    1. `script_model` explícito (kwarg)
    2. Fontes posicionais (da esquerda para a direita):
       - Se str: valor direto
       - Se dict: top-level `script_model` -> nested `campaign.script_model`
       - Se objeto com atributo `script_model`: valor do atributo
    """
    if script_model is not None and str(script_model).strip():
        return str(script_model).strip()
    for source in sources:
        if source is None:
            continue
        if isinstance(source, str) and source.strip():
            return source.strip()
        if isinstance(source, dict):
            top = source.get("script_model")
            if top is not None and str(top).strip():
                return str(top).strip()
            campaign = source.get("campaign")
            if isinstance(campaign, dict):
                nested = campaign.get("script_model")
                if nested is not None and str(nested).strip():
                    return str(nested).strip()
            elif hasattr(campaign, "script_model"):
                nested = getattr(campaign, "script_model", None)
                if nested is not None and str(nested).strip():
                    return str(nested).strip()
        elif hasattr(source, "script_model"):
            attr = getattr(source, "script_model", None)
            if attr is not None and str(attr).strip():
                return str(attr).strip()
    return None


def apply_script_model_override(
    catalog: AgentCatalog,
    *sources: Any,
    script_model: str | None = None,
) -> AgentCatalog:
    """Aplica override de script_model extraído das fontes ao catálogo de forma fail-closed.

    Se nenhum modelo for especificado, retorna o catálogo inalterado.
    Se o modelo extraído não estiver presente em `allowed_models` para o stage 'scripts',
    levanta `ValueError`.
    """
    model = extract_script_model(*sources, script_model=script_model)
    return with_stage_model(catalog, "scripts", model)
