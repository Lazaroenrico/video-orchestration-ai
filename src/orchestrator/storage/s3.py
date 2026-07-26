"""Backend AWS S3 usando o mesmo contrato de mídia do R2."""
from __future__ import annotations

import os

import boto3

from orchestrator.storage.r2 import R2MediaStorage


class S3MediaStorage(R2MediaStorage):
    """S3 privado; credenciais vêm do task role/SDK credential chain."""

    backend = "s3"

    def __init__(self, *, bucket: str, client) -> None:
        super().__init__(
            bucket=bucket,
            client=client,
            backend=self.backend,
            uri_scheme="s3",
        )

    @classmethod
    def from_env(cls) -> "S3MediaStorage":
        bucket = os.environ.get("S3_BUCKET", "")
        region = os.environ.get("AWS_REGION", "")
        missing = [
            name
            for name, value in (("S3_BUCKET", bucket), ("AWS_REGION", region))
            if not value
        ]
        if missing:
            raise ValueError(
                "S3MediaStorage.from_env: variável de ambiente ausente: "
                + ", ".join(missing)
            )
        return cls(
            bucket=bucket,
            client=boto3.client("s3", region_name=region),
        )
