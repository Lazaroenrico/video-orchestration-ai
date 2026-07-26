"""Roteador temporário para leitura R2+S3 durante o cutover AWS."""
from __future__ import annotations

from typing import Any


class MultiBackendMediaStorage:
    """Escreve no backend novo e lê cada ponteiro no backend que o originou."""

    def __init__(self, backends: dict[str, Any], *, write_backend: str) -> None:
        if write_backend not in backends:
            raise ValueError(f"write backend desconhecido: {write_backend!r}")
        self.backends = dict(backends)
        self.backend = write_backend

    def _storage(self, backend: str) -> Any:
        try:
            return self.backends[backend]
        except KeyError as exc:
            raise ValueError(f"storage backend desconhecido: {backend!r}") from exc

    async def put_bytes(self, data: bytes, *, key_base: str, content_type: str):
        return await self._storage(self.backend).put_bytes(
            data,
            key_base=key_base,
            content_type=content_type,
        )

    async def put_from_url(self, uri: str, *, key_base: str, client=None):
        return await self._storage(self.backend).put_from_url(
            uri,
            key_base=key_base,
            client=client,
        )

    async def get_signed_url(self, key: str, *, ttl_seconds: int = 900) -> str:
        return await self.get_signed_url_for(
            self.backend,
            key,
            ttl_seconds=ttl_seconds,
        )

    async def get_signed_url_for(
        self,
        backend: str,
        key: str,
        *,
        ttl_seconds: int = 900,
    ) -> str:
        return await self._storage(backend).get_signed_url(
            key,
            ttl_seconds=ttl_seconds,
        )

    async def delete(self, key: str) -> None:
        await self.delete_from(self.backend, key)

    async def delete_from(self, backend: str, key: str) -> None:
        await self._storage(backend).delete(key)

    async def exists(self, key: str) -> bool:
        return await self.exists_in(self.backend, key)

    async def exists_in(self, backend: str, key: str) -> bool:
        return await self._storage(backend).exists(key)
