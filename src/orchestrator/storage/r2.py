"""Backend de storage em Cloudflare R2, via API S3-compatible (D30).

Usado no perfil live. O R2 **não guarda estado de negócio** — só objetos binários; a
verdade sobre eles vive no ``ArtifactDB`` (``storage/db.py``). Por isso o ``uri`` de um
objeto aqui é o ponteiro canônico ``r2://{bucket}/{key}``, e **não** uma signed URL:
URL assinada expira, ponteiro não.

Async: o boto3 é síncrono, então cada chamada de rede vai para ``asyncio.to_thread``.
Diferente do SQLite do checkpointer (chamadas locais e curtas), aqui é upload de vídeo
— segurar o event loop travaria o fan-out paralelo de items.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any, Optional

import boto3
import httpx
from botocore.exceptions import ClientError

from orchestrator.storage.base import StoredObject, ext_from_mime, fetch_media

_log = logging.getLogger(__name__)

_ENV_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
_DEFAULT_TTL_SECONDS = 900


class R2MediaStorage:
    """Persiste bytes num bucket R2. O bucket permanece privado (D30)."""

    backend = "r2"

    def __init__(self, *, bucket: str, client: Any) -> None:
        self.bucket = bucket
        self._client = client

    @classmethod
    def from_env(cls) -> "R2MediaStorage":
        """Constrói a partir das envs de R2, falhando no boot se faltar credencial.

        Falhar cedo é deliberado: descobrir credencial ausente no meio de um run pago
        significa mídia gerada (e cobrada) sem lugar canônico para pousar.
        """
        missing = [var for var in _ENV_VARS if not os.environ.get(var)]
        if missing:
            raise ValueError(f"R2MediaStorage.from_env: variável de ambiente ausente: {', '.join(missing)}")

        account_id = os.environ["R2_ACCOUNT_ID"]
        # R2_ENDPOINT_URL permite apontar para outro endpoint S3-compatible (MinIO no
        # dev local, S3 na migração AWS da ADR-D36) sem tocar no código de domínio.
        endpoint_url = os.environ.get("R2_ENDPOINT_URL") or f"https://{account_id}.r2.cloudflarestorage.com"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",  # R2 não tem regiões no sentido da AWS
        )
        return cls(bucket=os.environ["R2_BUCKET"], client=client)

    def _stored(self, key: str, data: bytes, content_type: str) -> StoredObject:
        return StoredObject(
            backend=self.backend,
            key=key,
            uri=f"r2://{self.bucket}/{key}",
            content_type=content_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    async def _put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return self._stored(key, data, content_type)

    async def put_bytes(self, data: bytes, *, key_base: str, content_type: str) -> StoredObject:
        return await self._put(f"{key_base}.{ext_from_mime(content_type)}", data, content_type)

    async def put_from_url(
        self,
        uri: str,
        *,
        key_base: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Optional[StoredObject]:
        fetched = await fetch_media(uri, client=client)
        if fetched is None:
            return None
        return await self._put(f"{key_base}.{fetched.ext}", fetched.data, fetched.content_type)

    async def get_signed_url(self, key: str, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> str:
        """URL de leitura com TTL curto, derivada sob demanda. Não persistir (D30)."""
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            # Só "não existe" vira False. 403 (credencial) e afins têm de propagar —
            # tratar tudo como ausente esconderia bug de config atrás de um re-upload.
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True
