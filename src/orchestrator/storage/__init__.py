"""Abstração de storage de mídia (D30).

``LocalMediaStorage`` (mock/dry-run/dev/testes, sem rede) e — a partir da Fase 3 —
``R2MediaStorage`` (live) implementam o mesmo contrato. LangGraph, nodes, tools e
adapters continuam operando com ``Artifact`` e metadados normalizados, sem conhecer
detalhes do backend.
"""
from __future__ import annotations

from orchestrator.storage.base import MediaStorage, StoredObject
from orchestrator.storage.local import LocalMediaStorage

__all__ = ["LocalMediaStorage", "MediaStorage", "StoredObject"]
