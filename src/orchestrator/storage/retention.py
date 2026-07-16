"""Política de retenção de mídia (D30).

A D30 define o que sobrevive e o que é descartável:

- **Creator assets, clips aprovados e vídeos finais montados**: retidos, sem expiração.
- **Clips reprovados**: short-lived, expiram em **3 dias**.
- **Tentativas intermediárias**: short-lived, expiram em **2 dias**.

A limpeza é orientada por **metadata do DB**, nunca por varredura cega do bucket: o
bucket não sabe o que é um clip aprovado, o DB sabe. Varrer objetos por idade apagaria
o vídeo final de um run antigo.

``now`` é sempre injetado (nada de ``datetime.now()`` escondido), o que mantém a
expiração testável e determinística — a mesma regra do CLAUDE.md que proíbe ``random``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

RETENTION_KEEP = "keep"
RETENTION_REJECTED = "rejected"
RETENTION_INTERMEDIATE = "intermediate"

# TTL por classe. Ausência da chave = retido para sempre (``keep``).
# ``rejected`` e ``intermediate`` são ambos short-lived, mas continuam classes
# distintas: os prazos diferem e a diferença é auditável ("por que isso sumiu?").
_TTL_DAYS: dict[str, int] = {
    RETENTION_REJECTED: 3,
    RETENTION_INTERMEDIATE: 2,
}

_CLASSES = frozenset({RETENTION_KEEP, *_TTL_DAYS})


def expires_at_for(retention_class: str, *, now: datetime) -> Optional[str]:
    """Data de expiração ISO-8601 para a classe, ou ``None`` se ela é retida.

    Classe desconhecida levanta: cair no ``None`` por omissão transformaria um typo em
    "nunca expira", que é o modo de falha caro (bytes pagos acumulando para sempre).
    """
    if retention_class not in _CLASSES:
        raise ValueError(f"unknown retention class {retention_class!r}")
    days = _TTL_DAYS.get(retention_class)
    return None if days is None else (now + timedelta(days=days)).isoformat()


async def purge_expired(db: Any, storage: Any, *, now: datetime) -> list[str]:
    """Apaga bytes e linha de todo artifact já expirado. Devolve as keys removidas.

    Ordem deliberada: **bytes primeiro, linha depois**. Se a remoção do objeto falhar, a
    linha continua lá e o próximo purge tenta de novo. O inverso perderia o ponteiro e
    deixaria o objeto órfão no bucket, cobrando storage que ninguém mais sabe nomear.
    """
    purged: list[str] = []
    for artifact in await db.expired(now=now):
        await storage.delete(artifact.storage_key)
        await db.delete(artifact.id)
        purged.append(artifact.storage_key)
    return purged
