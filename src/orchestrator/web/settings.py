"""Configuração derivada de ambiente compartilhada pelos módulos web.

Ponto único para os defaults de config dir e para leitura de flags booleanas
de ambiente usadas pelo servidor e pelas rotas. Sem estado mutável.
"""

from __future__ import annotations

import os
from typing import Optional

WEB_DEFAULT_CONFIG_DIR = "config-staging"


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def effective_config_dir(config_dir: Optional[str]) -> str:
    return config_dir or os.environ.get("ORCH_CONFIG_DIR") or WEB_DEFAULT_CONFIG_DIR
