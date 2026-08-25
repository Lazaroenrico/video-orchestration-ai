"""Registros canônicos de artifacts — tipos de domínio compartilhados.

Módulo neutro na disputa entre ``orchestrator.storage`` e ``orchestrator.db``:
ambos importam daqui, e nenhum deles é importado de volta. Isso quebra o ciclo
db ↔ storage que existia quando ``db/artifacts.py`` buscava ``ArtifactRecord``
de ``storage/db.py`` enquanto este importava o repositório PostgreSQL de lá.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from orchestrator.storage.base import StoredObject
from orchestrator.storage.retention import RETENTION_KEEP


@dataclass(frozen=True)
class ArtifactRecord:
    """Uma linha canônica de artifact. Espelha as colunas mínimas da ADR-D30."""

    run_id: str
    kind: str
    storage_backend: str
    storage_key: str
    item_id: Optional[str] = None
    creator_id: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    source_uri: Optional[str] = None
    retention_class: str = RETENTION_KEEP
    expires_at: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Id determinístico derivado do ponteiro canônico.

        Determinismo (CLAUDE.md): nada de ``uuid4``. Como ``storage_key`` já é único por
        run/item/kind, derivar o id dele torna ``record`` idempotente de graça — um
        retry que re-persiste os mesmos bytes atualiza a linha em vez de duplicá-la.
        """
        return hashlib.sha256(f"{self.run_id}:{self.storage_key}".encode()).hexdigest()[:32]

    @classmethod
    def from_stored(
        cls,
        stored: StoredObject,
        *,
        run_id: str,
        kind: str,
        item_id: Optional[str] = None,
        creator_id: Optional[str] = None,
        source_uri: Optional[str] = None,
        retention_class: str = RETENTION_KEEP,
        expires_at: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> "ArtifactRecord":
        """Ponte com a Fase 1: o ``StoredObject`` já traz backend/key/hash/tamanho."""
        return cls(
            run_id=run_id,
            kind=kind,
            storage_backend=stored.backend,
            storage_key=stored.key,
            item_id=item_id,
            creator_id=creator_id,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            source_uri=source_uri,
            retention_class=retention_class,
            expires_at=expires_at,
            meta=meta or {},
        )
