"""Cópia verificável de objetos entre backends S3-compatible."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import os
from typing import Any, Protocol

import boto3


@dataclass(frozen=True)
class ObjectHead:
    size_bytes: int
    sha256: str


class ObjectTransferStore(Protocol):
    backend: str

    async def get_object(self, key: str) -> bytes: ...

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None: ...

    async def head_object(self, key: str) -> ObjectHead: ...


class ObjectIntegrityError(RuntimeError):
    """Os bytes lidos ou gravados não correspondem ao checksum canônico."""


class BotoObjectStore:
    """Fronteira mínima Get/Put/Head com key exata para R2 ou S3."""

    def __init__(
        self,
        *,
        backend: str,
        bucket: str,
        client: Any,
        native_checksum: bool,
    ) -> None:
        self.backend = backend
        self.bucket = bucket
        self._client = client
        self._native_checksum = native_checksum

    @classmethod
    def from_r2_env(cls) -> "BotoObjectStore":
        names = (
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET",
        )
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise ValueError("R2 transfer env ausente: " + ", ".join(missing))
        account_id = os.environ["R2_ACCOUNT_ID"]
        endpoint = os.environ.get("R2_ENDPOINT_URL") or (
            f"https://{account_id}.r2.cloudflarestorage.com"
        )
        return cls(
            backend="r2",
            bucket=os.environ["R2_BUCKET"],
            client=boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
            ),
            native_checksum=False,
        )

    @classmethod
    def from_s3_env(cls) -> "BotoObjectStore":
        names = ("S3_BUCKET", "AWS_REGION")
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise ValueError("S3 transfer env ausente: " + ", ".join(missing))
        return cls(
            backend="s3",
            bucket=os.environ["S3_BUCKET"],
            client=boto3.client("s3", region_name=os.environ["AWS_REGION"]),
            native_checksum=True,
        )

    async def get_object(self, key: str) -> bytes:
        def _read() -> bytes:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()

        return await asyncio.to_thread(_read)

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "Metadata": metadata,
        }
        if self._native_checksum:
            digest = bytes.fromhex(metadata["sha256"])
            arguments["ChecksumSHA256"] = base64.b64encode(digest).decode("ascii")
        await asyncio.to_thread(self._client.put_object, **arguments)

    async def head_object(self, key: str) -> ObjectHead:
        response = await asyncio.to_thread(
            self._client.head_object,
            Bucket=self.bucket,
            Key=key,
        )
        return ObjectHead(
            size_bytes=int(response["ContentLength"]),
            sha256=response.get("Metadata", {}).get("sha256", ""),
        )


async def migrate_run_objects(
    repository: Any,
    *,
    run_id: str,
    source: ObjectTransferStore,
    destination: ObjectTransferStore,
) -> dict[str, Any]:
    """Copia um run; só muda ``storage_backend`` após HEAD verificado."""
    copied: list[str] = []
    skipped: list[str] = []
    for artifact in await repository.by_run(run_id):
        if artifact.storage_backend != source.backend:
            skipped.append(artifact.storage_key)
            continue

        data = await source.get_object(artifact.storage_key)
        digest = hashlib.sha256(data).hexdigest()
        if artifact.sha256 and digest != artifact.sha256:
            raise ObjectIntegrityError(
                f"checksum da origem diverge para {artifact.storage_key!r}"
            )
        metadata = {
            "sha256": digest,
            "run-id": artifact.run_id,
            "artifact-id": artifact.id,
            "source-backend": source.backend,
        }
        await destination.put_object(
            artifact.storage_key,
            data,
            content_type=artifact.content_type or "application/octet-stream",
            metadata=metadata,
        )
        head = await destination.head_object(artifact.storage_key)
        if head.size_bytes != len(data) or head.sha256 != digest:
            raise ObjectIntegrityError(
                f"verificação do destino diverge para {artifact.storage_key!r}"
            )
        await repository.set_storage_backend(
            artifact.storage_key,
            destination.backend,
        )
        copied.append(artifact.storage_key)

    return {
        "run_id": run_id,
        "source_backend": source.backend,
        "destination_backend": destination.backend,
        "copied": copied,
        "skipped": skipped,
    }
