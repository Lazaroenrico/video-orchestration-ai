"""Carga dos arquivos de configuração (YAML) e caminhos padrão."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from orchestrator.agent_catalog import AgentCatalog, build_agent_catalog, default_agent_catalog

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")

# Diretório irmão dos perfis com o conteúdo compartilhado. Cada perfil
# (config/, config-mock/, config-staging/) mantém apenas os overrides;
# o loader funde base + perfil (deep-merge, overlay vence; listas são
# substituídas wholesale).
_BASE_CONFIG_DIRNAME = "config-base"


def _expand_env(text: str) -> str:
    """Resolve placeholders ${VAR} e ${VAR:-default} a partir do ambiente."""

    def repl(m: re.Match[str]) -> str:
        var, default = m.group(1), m.group(2)
        return os.environ.get(var, default if default is not None else "")

    return _ENV_RE.sub(repl, text)


def config_dir(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get("ORCH_CONFIG_DIR", "config"))


def config_base_dir(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Base compartilhada do perfil (irmã do config dir); None se não existir."""
    candidate = config_dir(path).parent / _BASE_CONFIG_DIRNAME
    return candidate if candidate.is_dir() else None


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Fusão recursiva: overlay vence por chave; listas e escalares são substituídos."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path, expand: bool = True) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if expand:
        text = _expand_env(text)
    return yaml.safe_load(text) or {}


def _load_merged_yaml(name: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Carrega <perfil>/<name> fundido sobre <config-base>/<name> (se houver)."""
    data = _load_yaml(config_dir(path) / name)
    base = config_base_dir(path)
    if base is not None:
        base_path = base / name
        if base_path.exists():
            data = _deep_merge(_load_yaml(base_path), data)
    return data


def load_pipeline(path: str | None = None) -> dict[str, Any]:
    return _load_merged_yaml("pipeline.yaml", path)


def load_providers(path: str | None = None) -> dict[str, Any]:
    return _load_merged_yaml("providers.yaml", path)


def load_judge(path: str | None = None) -> dict[str, Any]:
    # judge.yaml tem placeholders de ambiente (url/key do gateway).
    return _load_merged_yaml("judge.yaml", path)


def load_agent_catalog(path: str | None = None) -> AgentCatalog:
    profile = config_dir(path)
    catalog_path = profile / "agents.yaml"
    base = config_base_dir(path)
    base_catalog_path = base / "agents.yaml" if base is not None else None
    if not catalog_path.exists() and (base_catalog_path is None or not base_catalog_path.exists()):
        return default_agent_catalog()
    fallback_dirs = (base,) if base is not None else ()
    return build_agent_catalog(
        _load_merged_yaml("agents.yaml", path),
        base_dir=profile,
        fallback_dirs=fallback_dirs,
    )


def default_db_path() -> Path:
    return Path(os.environ.get("ORCH_DB", ".orchestrator/runs.sqlite"))


def default_creator_store_path() -> Path:
    return Path(os.environ.get("ORCH_CREATORS", ".orchestrator/creators.json"))


def default_prompt_store_path() -> Path:
    return Path(os.environ.get("ORCH_PROMPTS", ".orchestrator/prompts.json"))


def default_artifacts_db_path() -> Path:
    """DB canônico de artifacts (D30). Separado do checkpointer: um guarda estado do
    grafo, o outro guarda a verdade sobre mídia — ciclos de vida diferentes."""
    return Path(os.environ.get("ORCH_ARTIFACTS_DB", ".orchestrator/artifacts.sqlite"))


def default_media_path() -> Path:
    return Path(os.environ.get("ORCH_MEDIA", ".orchestrator/media"))


def default_videos_path() -> Path:
    return Path(os.environ.get("ORCH_VIDEOS", ".orchestrator/videos"))
