"""Abstração de storage de mídia (D30).

``LocalMediaStorage`` (mock/dry-run/dev/testes, sem rede) e — a partir da Fase 3 —
``R2MediaStorage`` (live) implementam o mesmo contrato. LangGraph, nodes, tools e
adapters continuam operando com ``Artifact`` e metadados normalizados, sem conhecer
detalhes do backend.
"""
from __future__ import annotations

from orchestrator.storage.base import MediaStorage, StoredObject
from orchestrator.storage.db import ArtifactDB, ArtifactRecord
from orchestrator.storage.factory import build_media_storage
from orchestrator.storage.local import LocalMediaStorage
from orchestrator.storage.r2 import R2MediaStorage

__all__ = [
    "ArtifactDB",
    "ArtifactRecord",
    "LocalMediaStorage",
    "MediaStorage",
    "R2MediaStorage",
    "StoredObject",
    "build_media_storage",
]
