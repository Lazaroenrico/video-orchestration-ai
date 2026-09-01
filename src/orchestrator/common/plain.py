"""Conversão pydantic/containers -> estruturas JSON-like (fonte única)."""

from __future__ import annotations

from typing import Any


def to_plain(value: Any) -> Any:
    """Reduz pydantic models e containers a estruturas JSON-serializáveis.

    - Models (qualquer objeto com ``model_dump``) são convertidos em ``mode="json"``
      recursivamente, garantindo datas/UUIDs/bytes em forma serializável.
    - Dicts têm as chaves stringificadas; listas e tuplas viram listas.
    - Escalares e objetos não reconhecidos passam intactos.
    """
    if hasattr(value, "model_dump"):
        return to_plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value
