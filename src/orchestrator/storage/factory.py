"""Resolução do backend de storage a partir de ``providers.yaml`` (D30).

Storage não é um **papel** do ``registry.py``: papéis (llm, creator, video, ...) mapeiam
para adapters que falam com modelos; storage é onde os bytes pousam, ortogonal a isso.
Por isso a chave vive fora de ``adapters:``.

    storage:
      backend: local   # default; ou "r2" no perfil live

Manter o default em ``local`` é o que preserva o critério de aceite da D30:
``config-mock`` segue offline, determinístico e sem custo mesmo sem declarar nada.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from orchestrator.storage.base import MediaStorage
from orchestrator.storage.local import LocalMediaStorage
from orchestrator.storage.r2 import R2MediaStorage

_DEFAULT_BACKEND = "local"


def build_media_storage(
    providers: Optional[dict[str, Any]],
    *,
    root: str | Path,
    web_prefix: str,
) -> MediaStorage:
    """Constrói o backend declarado em ``providers``.

    ``root``/``web_prefix`` só fazem sentido para o backend local (onde o dashboard
    serve os bytes do disco); o R2 deriva tudo das envs e ignora ambos.
    """
    backend = ((providers or {}).get("storage") or {}).get("backend", _DEFAULT_BACKEND)

    if backend == "local":
        return LocalMediaStorage(root, web_prefix=web_prefix)
    if backend == "r2":
        return R2MediaStorage.from_env()
    # Falha alto: um typo em providers.yaml degradando para disco local em produção
    # significaria mídia paga apodrecendo num container efêmero.
    raise ValueError(f"providers.yaml: unknown storage backend {backend!r}")
