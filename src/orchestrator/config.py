"""Carga dos arquivos de configuração (YAML) e caminhos padrão."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    """Resolve placeholders ${VAR} e ${VAR:-default} a partir do ambiente."""

    def repl(m: re.Match[str]) -> str:
        var, default = m.group(1), m.group(2)
        return os.environ.get(var, default if default is not None else "")

    return _ENV_RE.sub(repl, text)


def config_dir(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get("ORCH_CONFIG_DIR", "config"))


def _load_yaml(path: Path, expand: bool = False) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if expand:
        text = _expand_env(text)
    return yaml.safe_load(text) or {}


def load_pipeline(path: str | None = None) -> dict[str, Any]:
    return _load_yaml(config_dir(path) / "pipeline.yaml")


def load_providers(path: str | None = None) -> dict[str, Any]:
    return _load_yaml(config_dir(path) / "providers.yaml")


def load_judge(path: str | None = None) -> dict[str, Any]:
    # judge.yaml tem placeholders de ambiente (url/key do gateway).
    return _load_yaml(config_dir(path) / "judge.yaml", expand=True)


def default_db_path() -> Path:
    return Path(os.environ.get("ORCH_DB", ".orchestrator/runs.sqlite"))


def default_creator_store_path() -> Path:
    return Path(os.environ.get("ORCH_CREATORS", ".orchestrator/creators.json"))


def default_prompt_store_path() -> Path:
    return Path(os.environ.get("ORCH_PROMPTS", ".orchestrator/prompts.json"))


def default_media_path() -> Path:
    return Path(os.environ.get("ORCH_MEDIA", ".orchestrator/media"))


def default_videos_path() -> Path:
    return Path(os.environ.get("ORCH_VIDEOS", ".orchestrator/videos"))
