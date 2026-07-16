"""Testes do R2MediaStorage (D30, Fase 3).

Offline por construção: o client S3 é injetado como stub em memória. Nenhuma credencial
de R2 é necessária — critério de aceite da ADR-D30 ("a suíte offline continua verde sem
credenciais de R2").
"""
from __future__ import annotations

import base64

import httpx
import pytest
from botocore.exceptions import ClientError

from orchestrator.storage.base import MediaStorage
from orchestrator.storage.r2 import R2MediaStorage

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


class _FakeS3:
    """Stub do client boto3 S3: guarda objetos num dict e registra as chamadas."""

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.presigned: list[dict] = []

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803 — assinatura do boto3
        self.objects[Key] = {"bucket": Bucket, "body": Body, "content_type": ContentType}

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]["body"])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def generate_presigned_url(self, op, *, Params, ExpiresIn):  # noqa: N803
        self.presigned.append({"op": op, "params": Params, "expires_in": ExpiresIn})
        return f"https://signed.example/{Params['Key']}?ttl={ExpiresIn}"


@pytest.fixture
def s3() -> _FakeS3:
    return _FakeS3()


@pytest.fixture
def storage(s3) -> R2MediaStorage:
    return R2MediaStorage(bucket="ugc", client=s3)


def _ok_transport(content: bytes, content_type: str = "video/mp4") -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, content=content, headers={"content-type": content_type})
    )


# --------------------------------------------------------------------------- #
# Contrato                                                                     #
# --------------------------------------------------------------------------- #


def test_r2_storage_satisfies_the_media_storage_protocol(storage):
    assert isinstance(storage, MediaStorage)


def test_r2_storage_declares_its_backend_name(storage):
    assert storage.backend == "r2"


# --------------------------------------------------------------------------- #
# put                                                                          #
# --------------------------------------------------------------------------- #


async def test_put_bytes_uploads_to_the_bucket_and_returns_canonical_metadata(storage, s3):
    stored = await storage.put_bytes(_PNG_BYTES, key_base="run-1/c0/image", content_type="image/png")

    assert stored.backend == "r2"
    assert stored.key == "run-1/c0/image.png"
    assert stored.content_type == "image/png"
    assert stored.size_bytes == len(_PNG_BYTES)
    assert s3.objects["run-1/c0/image.png"]["body"] == _PNG_BYTES
    assert s3.objects["run-1/c0/image.png"]["content_type"] == "image/png"
    assert s3.objects["run-1/c0/image.png"]["bucket"] == "ugc"


async def test_put_returns_a_canonical_r2_uri_not_a_signed_url(storage):
    """D30: signed URL não é valor canônico — o Artifact carrega o ponteiro."""
    stored = await storage.put_bytes(_PNG_BYTES, key_base="run-1/c0/image", content_type="image/png")

    assert stored.uri == "r2://ugc/run-1/c0/image.png"
    assert "signed.example" not in stored.uri


async def test_put_from_url_downloads_then_uploads_the_bytes(storage, s3):
    client = httpx.AsyncClient(transport=_ok_transport(b"\x00mp4", "video/mp4"))

    stored = await storage.put_from_url(
        "https://replicate.delivery/out.mp4", key_base="run-1/items/i0/clip-0", client=client,
    )

    assert stored.key == "run-1/items/i0/clip-0.mp4"
    assert s3.objects["run-1/items/i0/clip-0.mp4"]["body"] == b"\x00mp4"
    await client.aclose()


async def test_put_from_url_handles_a_data_uri_without_network(storage, s3):
    stored = await storage.put_from_url(_PNG_DATA_URI, key_base="run-1/c0/image")

    assert stored.key == "run-1/c0/image.png"
    assert s3.objects["run-1/c0/image.png"]["body"] == _PNG_BYTES


@pytest.mark.parametrize("uri", ["mock://image/abc", "voice-0", ""])
async def test_put_from_url_is_a_noop_for_a_non_downloadable_reference(storage, s3, uri):
    assert await storage.put_from_url(uri, key_base="run-1/c0/image") is None
    assert s3.objects == {}


async def test_put_from_url_returns_none_and_uploads_nothing_when_the_download_fails(storage, s3):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    )

    assert await storage.put_from_url("https://x/a.mp4", key_base="run-1/c0/image", client=client) is None
    assert s3.objects == {}
    await client.aclose()


# --------------------------------------------------------------------------- #
# get_signed_url / delete / exists                                             #
# --------------------------------------------------------------------------- #


async def test_get_signed_url_derives_a_short_lived_url_from_the_key(storage, s3):
    url = await storage.get_signed_url("run-1/c0/image.png", ttl_seconds=300)

    assert url == "https://signed.example/run-1/c0/image.png?ttl=300"
    assert s3.presigned == [
        {"op": "get_object", "params": {"Bucket": "ugc", "Key": "run-1/c0/image.png"}, "expires_in": 300}
    ]


async def test_get_signed_url_has_a_short_default_ttl(storage, s3):
    await storage.get_signed_url("run-1/c0/image.png")

    assert s3.presigned[0]["expires_in"] == 900


async def test_exists_is_true_after_upload_and_false_for_a_missing_key(storage):
    await storage.put_bytes(b"x", key_base="run-1/c0/image", content_type="image/png")

    assert await storage.exists("run-1/c0/image.png") is True
    assert await storage.exists("run-1/c0/missing.png") is False


async def test_exists_propagates_an_error_that_is_not_a_missing_object(storage, s3):
    """403 é problema de credencial, não 'não existe' — engolir viraria bug silencioso."""

    def denied(*, Bucket, Key):  # noqa: N803
        raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

    s3.head_object = denied

    with pytest.raises(ClientError):
        await storage.exists("run-1/c0/image.png")


async def test_delete_removes_the_object(storage, s3):
    await storage.put_bytes(b"x", key_base="run-1/c0/image", content_type="image/png")

    await storage.delete("run-1/c0/image.png")

    assert s3.objects == {}


# --------------------------------------------------------------------------- #
# Construção a partir de env                                                   #
# --------------------------------------------------------------------------- #


def test_from_env_builds_the_s3_client_against_the_r2_endpoint(monkeypatch):
    import orchestrator.storage.r2 as r2_module

    captured: dict = {}

    def fake_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return _FakeS3()

    monkeypatch.setattr(r2_module.boto3, "client", fake_client)
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_BUCKET", "ugc-prod")

    storage = R2MediaStorage.from_env()

    assert storage.bucket == "ugc-prod"
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://acct123.r2.cloudflarestorage.com"
    assert captured["aws_access_key_id"] == "ak"
    assert captured["aws_secret_access_key"] == "sk"


@pytest.mark.parametrize(
    "missing", ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
)
def test_from_env_fails_fast_when_a_credential_is_missing(monkeypatch, missing):
    """Falhar no boot é melhor que descobrir credencial faltando no meio de um run pago."""
    for var in ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]:
        monkeypatch.setenv(var, "x")
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match=missing):
        R2MediaStorage.from_env()
