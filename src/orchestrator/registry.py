"""Resolução de adapters a partir dos configs (provider -> adapter).

No v1 só existe o ``mock``. Para ligar um provedor real, registre aqui o adapter
(implementando os Protocols de ``adapters/base.py``) e troque o nome em
``config/providers.yaml`` — o grafo não muda.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.replicate_video import ReplicateVideoAdapter


def _build_replicate(pipeline: dict[str, Any]) -> ReplicateVideoAdapter:
    """Fábrica do ReplicateVideoAdapter — lê token de REPLICATE_API_TOKEN."""
    return ReplicateVideoAdapter(
        tiers=pipeline["tiers"],
        token=os.environ.get("REPLICATE_API_TOKEN", ""),
    )


# nome -> fábrica de adapter
_ADAPTERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "mock": lambda pipeline: MockAdapter(
        tiers=pipeline["tiers"], latency=float(pipeline.get("latency", 0.0))
    ),
    "replicate": _build_replicate,
}


def resolve_adapter(name: str, pipeline: dict[str, Any]) -> Any:
    """Instancia o adapter pelo nome (ex.: 'mock')."""
    if name not in _ADAPTERS:
        raise KeyError(f"adapter desconhecido: {name!r} (registrados: {sorted(_ADAPTERS)})")
    return _ADAPTERS[name](pipeline)


def register_adapter(name: str, factory: Callable[[dict[str, Any]], Any]) -> None:
    """Registra um adapter real (chamado por quem for plugar Claude/ElevenLabs/etc.)."""
    _ADAPTERS[name] = factory


def build_adapter_from_providers(
    providers: dict[str, Any], pipeline: dict[str, Any]
) -> Any:
    """No v1 todos os papéis usam um único adapter; usa o mapeado em 'video'."""
    name = providers.get("adapters", {}).get("video", "mock")
    return resolve_adapter(name, pipeline)
