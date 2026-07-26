"""Resolução de ponteiros canônicos em signed URLs (D30).

A D30 é explícita: signed URLs são geradas **só quando um consumidor precisa dos bytes**
(preview na UI, download, handoff para provider externo) e **não substituem o
``storage_key``** como verdade. Este módulo é exatamente essa fronteira: o estado e o DB
guardam ``r2://{bucket}/{key}``; a URL assinada só existe no payload de saída, e expira.

Por isso a resolução é uma **cópia**, nunca uma mutação: se ela escrevesse a URL de volta
no estado, o checkpoint passaria a guardar uma URL vencida como se fosse o ponteiro.
"""
from __future__ import annotations

from typing import Any, Optional

_OBJECT_SCHEMES = ("r2", "s3")
_DEFAULT_TTL_SECONDS = 900


def object_pointer_from_uri(uri: Any) -> Optional[tuple[str, str]]:
    """Extrai ``(backend, key)`` de ponteiros canônicos R2/S3."""
    if not isinstance(uri, str):
        return None
    for backend in _OBJECT_SCHEMES:
        prefix = f"{backend}://"
        if uri.startswith(prefix):
            _, _, rest = uri.partition(prefix)
            _, sep, key = rest.partition("/")
            return (backend, key) if sep and key else None
    return None


def r2_key_from_uri(uri: Any) -> Optional[str]:
    """``r2://{bucket}/{key}`` -> ``key``. Qualquer outra coisa -> ``None``.

    Paths locais (``/videos/...``), http(s), ``data:`` e referências opacas
    (``mock://``, ``voice-0``) não são ponteiros de objeto: passam intactos.
    """
    pointer = object_pointer_from_uri(uri)
    return pointer[1] if pointer is not None and pointer[0] == "r2" else None


async def resolve_signed_uris(
    payload: Any,
    *,
    storage: Any,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> Any:
    """Devolve uma cópia de ``payload`` com todo ponteiro ``r2://`` assinado.

    ``storage`` ``None`` (ou backend que não assina) devolve o payload como está — é o
    caso do backend local, cujos paths o dashboard já serve direto do disco.

    Cada key é assinada **uma vez** por chamada: o mesmo clip aparece em ``results`` e em
    ``artifacts``, e assinar de novo é HMAC jogado fora.
    """
    if storage is None or not (
        hasattr(storage, "get_signed_url")
        or hasattr(storage, "get_signed_url_for")
    ):
        return payload

    cache: dict[tuple[str, str], str] = {}

    async def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: await _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [await _walk(v) for v in node]
        pointer = object_pointer_from_uri(node)
        if pointer is None:
            return node
        backend, key = pointer
        if pointer not in cache:
            if hasattr(storage, "get_signed_url_for"):
                cache[pointer] = await storage.get_signed_url_for(
                    backend,
                    key,
                    ttl_seconds=ttl_seconds,
                )
            elif getattr(storage, "backend", "r2") == backend:
                cache[pointer] = await storage.get_signed_url(
                    key,
                    ttl_seconds=ttl_seconds,
                )
            else:
                return node
        return cache[pointer]

    return await _walk(payload)
