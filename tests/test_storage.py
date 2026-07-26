"""Testes da abstração de storage de mídia (D30).

Determinístico e offline: data URIs são decodificados em memória, downloads HTTP usam
``httpx.MockTransport`` (sem rede) e o backend local escreve em ``tmp_path``. Nenhuma
credencial de R2 é necessária — a suíte offline continua verde sem elas (critério de
aceite da ADR-D30).
"""
from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from orchestrator.storage.base import MediaStorage, StoredObject
from orchestrator.storage.local import LocalMediaStorage

# 1x1 PNG transparente (bytes reais), como base64 e como data URI.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()
_PNG_SHA256 = hashlib.sha256(_PNG_BYTES).hexdigest()


def _ok_transport(content: bytes, content_type: str = "image/png") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


def _fail_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    return httpx.MockTransport(handler)


def _storage(tmp_path) -> LocalMediaStorage:
    return LocalMediaStorage(root=tmp_path, web_prefix="/media")


# --------------------------------------------------------------------------- #
# Contrato                                                                     #
# --------------------------------------------------------------------------- #


def test_local_storage_satisfies_the_media_storage_protocol(tmp_path):
    assert isinstance(_storage(tmp_path), MediaStorage)


def test_local_storage_declares_its_backend_name(tmp_path):
    assert _storage(tmp_path).backend == "local"


# --------------------------------------------------------------------------- #
# put_bytes                                                                    #
# --------------------------------------------------------------------------- #


async def test_put_bytes_writes_the_file_and_returns_canonical_metadata(tmp_path):
    storage = _storage(tmp_path)

    stored = await storage.put_bytes(_PNG_BYTES, key_base="run-1/creator-0/image", content_type="image/png")

    assert isinstance(stored, StoredObject)
    assert stored.backend == "local"
    assert stored.key == "run-1/creator-0/image.png"
    assert stored.uri == "/media/run-1/creator-0/image.png"
    assert stored.content_type == "image/png"
    assert stored.size_bytes == len(_PNG_BYTES)
    assert stored.sha256 == _PNG_SHA256
    assert (tmp_path / "run-1/creator-0/image.png").read_bytes() == _PNG_BYTES


async def test_put_bytes_derives_the_extension_from_the_content_type(tmp_path):
    storage = _storage(tmp_path)

    stored = await storage.put_bytes(b"id3", key_base="run-1/creator-0/voice", content_type="audio/mpeg")

    assert stored.key.endswith(".mp3")


async def test_put_bytes_falls_back_to_bin_for_an_unknown_content_type(tmp_path):
    storage = _storage(tmp_path)

    stored = await storage.put_bytes(b"?", key_base="run-1/x/blob", content_type="application/x-weird")

    assert stored.key == "run-1/x/blob.bin"


# --------------------------------------------------------------------------- #
# put_from_url                                                                 #
# --------------------------------------------------------------------------- #


async def test_put_from_url_stores_the_bytes_of_a_data_uri_without_touching_the_network(tmp_path):
    storage = _storage(tmp_path)

    stored = await storage.put_from_url(_PNG_DATA_URI, key_base="run-1/creator-0/image")

    assert stored.key == "run-1/creator-0/image.png"
    assert stored.sha256 == _PNG_SHA256
    assert (tmp_path / stored.key).read_bytes() == _PNG_BYTES


async def test_put_from_url_downloads_http_and_takes_the_extension_from_the_url(tmp_path):
    storage = _storage(tmp_path)
    client = httpx.AsyncClient(transport=_ok_transport(_PNG_BYTES, "application/octet-stream"))

    stored = await storage.put_from_url(
        "https://cdn.example/asset.png", key_base="run-1/items/item-0/clip-0", client=client,
    )

    assert stored.key == "run-1/items/item-0/clip-0.png"
    assert stored.size_bytes == len(_PNG_BYTES)
    await client.aclose()


async def test_put_from_url_falls_back_to_the_content_type_when_the_url_has_no_extension(tmp_path):
    storage = _storage(tmp_path)
    client = httpx.AsyncClient(transport=_ok_transport(b"\x00\x00", "video/mp4"))

    stored = await storage.put_from_url(
        "https://cdn.example/download", key_base="run-1/items/item-0/clip-0", client=client,
    )

    assert stored.key == "run-1/items/item-0/clip-0.mp4"
    assert stored.content_type == "video/mp4"
    await client.aclose()


async def test_put_from_url_uses_its_own_client_when_none_is_injected(tmp_path, monkeypatch):
    """Produção não injeta client — o adapter cria (e fecha) o seu."""
    import orchestrator.storage.base as storage_base

    storage = _storage(tmp_path)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        storage_base.httpx, "AsyncClient",
        lambda *a, **k: real_async_client(transport=_ok_transport(_PNG_BYTES)),
    )

    stored = await storage.put_from_url("https://cdn.example/a.png", key_base="run-1/x/image")

    assert stored.sha256 == _PNG_SHA256


@pytest.mark.parametrize("uri", ["mock://image/abc", "voice-0", "", "s3://bucket/key"])
async def test_put_from_url_is_a_noop_for_a_non_downloadable_reference(tmp_path, uri):
    """mock:// e ids opacos são referência, não bytes — nada de disco, nada de rede."""
    storage = _storage(tmp_path)

    assert await storage.put_from_url(uri, key_base="run-1/x/image") is None
    assert not list(tmp_path.rglob("*"))


async def test_put_from_url_returns_none_and_writes_nothing_when_the_download_fails(tmp_path):
    storage = _storage(tmp_path)
    client = httpx.AsyncClient(transport=_fail_transport())

    assert await storage.put_from_url("https://cdn.example/a.png", key_base="run-1/x/image", client=client) is None
    assert not list(tmp_path.rglob("*"))
    await client.aclose()


# --------------------------------------------------------------------------- #
# get_signed_url / delete / exists                                             #
# --------------------------------------------------------------------------- #


async def test_get_signed_url_returns_the_servable_web_path_for_the_local_backend(tmp_path):
    """Local não assina: o dashboard já serve /media/... diretamente."""
    storage = _storage(tmp_path)

    assert await storage.get_signed_url("run-1/creator-0/image.png") == "/media/run-1/creator-0/image.png"


async def test_exists_reflects_whether_the_object_was_stored(tmp_path):
    storage = _storage(tmp_path)
    stored = await storage.put_bytes(_PNG_BYTES, key_base="run-1/x/image", content_type="image/png")

    assert await storage.exists(stored.key) is True
    assert await storage.exists("run-1/x/missing.png") is False


async def test_delete_removes_the_object_and_is_idempotent(tmp_path):
    storage = _storage(tmp_path)
    stored = await storage.put_bytes(_PNG_BYTES, key_base="run-1/x/image", content_type="image/png")

    await storage.delete(stored.key)
    assert await storage.exists(stored.key) is False

    await storage.delete(stored.key)  # já removido — não levanta


@pytest.mark.parametrize("key", ["../escape.png", "/abs/path.png", "run-1/../../etc/passwd"])
async def test_local_storage_rejects_a_key_that_escapes_the_root(tmp_path, key):
    """A key vem de run_id/item_id; tratá-la como não confiável evita escrita fora do root."""
    storage = _storage(tmp_path)

    with pytest.raises(ValueError, match="invalid storage key"):
        await storage.exists(key)
