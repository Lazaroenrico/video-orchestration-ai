"""Backend de storage em disco local (D30).

É o comportamento histórico do ``media_store``: bytes em ``ORCH_MEDIA``/``ORCH_VIDEOS``
e URIs reescritas para caminhos web servíveis (``/media/...``, ``/videos/...``). Usado
por mock, dry-run, desenvolvimento e testes — **não faz rede** além do download dos
bytes de origem, e nunca precisa de credencial.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

import httpx

from orchestrator.storage.base import (
    StoredObject,
    decode_data_uri,
    ext_from_mime,
    ext_from_url,
    is_downloadable,
)

_log = logging.getLogger(__name__)


class LocalMediaStorage:
    """Persiste em ``root`` e serve por ``web_prefix``."""

    backend = "local"

    def __init__(self, root: str | Path, *, web_prefix: str) -> None:
        self._root = Path(root)
        self._web_prefix = web_prefix.rstrip("/")

    def _resolve(self, key: str) -> Path:
        """Mapeia key -> path, recusando qualquer key que escape do root.

        A key é derivada de ``run_id``/``item_id``/``creator_id``, que vêm de config e de
        providers — entrada não confiável. Validar aqui mantém o invariante num ponto só.
        """
        candidate = Path(key)
        if not key or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"invalid storage key: {key!r}")
        return self._root / candidate

    def _stored(self, key: str, data: bytes, content_type: str) -> StoredObject:
        return StoredObject(
            backend=self.backend,
            key=key,
            uri=f"{self._web_prefix}/{key}",
            content_type=content_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    async def put_bytes(self, data: bytes, *, key_base: str, content_type: str) -> StoredObject:
        key = f"{key_base}.{ext_from_mime(content_type)}"
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._stored(key, data, content_type)

    async def put_from_url(
        self,
        uri: str,
        *,
        key_base: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Optional[StoredObject]:
        if not is_downloadable(uri):
            return None

        try:
            if uri.startswith("data:"):
                data, content_type = decode_data_uri(uri)
                ext = ext_from_mime(content_type)
            else:
                owns_client = client is None
                client = client or httpx.AsyncClient(timeout=120.0)
                try:
                    resp = await client.get(uri)
                    resp.raise_for_status()
                    data = resp.content
                    content_type = resp.headers.get("content-type", "")
                    ext = ext_from_url(uri) or ext_from_mime(content_type)
                finally:
                    if owns_client:
                        await client.aclose()
        except Exception as exc:  # noqa: BLE001 — download é best-effort
            _log.error("put_from_url falhou para %s: %s: %s", uri, type(exc).__name__, exc)
            return None

        key = f"{key_base}.{ext}"
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._stored(key, data, content_type)

    async def get_signed_url(self, key: str, *, ttl_seconds: int = 900) -> str:
        """Local não assina: o dashboard serve ``web_prefix`` diretamente do disco."""
        self._resolve(key)
        return f"{self._web_prefix}/{key}"

    async def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()
