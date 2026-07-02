"""Testes do media_store — download e persistência de bytes do creator.

Determinístico e offline: data URIs são decodificados em memória; downloads HTTP
usam ``httpx.MockTransport`` (sem rede). URIs não-baixáveis (mock://, voice_id) são
no-op — nunca tocam disco nem rede.
"""
from __future__ import annotations

import base64

import httpx
import pytest

from orchestrator import media_store

# 1x1 PNG transparente (bytes reais), como base64 e como data URI.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


def _ok_transport(content: bytes, content_type: str = "image/png") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


def _fail_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# _is_downloadable                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "uri,expected",
    [
        ("https://replicate.delivery/x/out.png", True),
        ("http://example.com/a.png", True),
        ("data:image/png;base64,AAAA", True),
        ("mock://creator/0/base_4k.png", False),
        ("voice-0", False),
        ("", False),
        ("file:///etc/passwd", False),
    ],
)
def test_is_downloadable(uri, expected):
    assert media_store._is_downloadable(uri) is expected


# --------------------------------------------------------------------------- #
# persist_media — data URI                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_persist_media_data_uri_writes_png(tmp_path):
    out = await media_store.persist_media(
        _PNG_DATA_URI, tmp_path / "run-1" / "creator-0", "image",
        web_prefix="/media/run-1/creator-0",
    )
    assert out == "/media/run-1/creator-0/image.png"
    written = (tmp_path / "run-1" / "creator-0" / "image.png").read_bytes()
    assert written == _PNG_BYTES


@pytest.mark.asyncio
async def test_persist_media_data_uri_svg_keeps_svg_extension(tmp_path):
    """SVG (ex.: imagem mock do creator) precisa de extensão .svg — servido como
    application/octet-stream via .bin, o browser não renderiza a imagem."""
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    out = await media_store.persist_media(
        uri, tmp_path / "c", "image", web_prefix="/media/c",
    )
    assert out == "/media/c/image.svg"
    assert (tmp_path / "c" / "image.svg").read_bytes() == svg


# --------------------------------------------------------------------------- #
# persist_media — HTTP                                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_persist_media_http_downloads_bytes(tmp_path):
    client = httpx.AsyncClient(transport=_ok_transport(_PNG_BYTES))
    out = await media_store.persist_media(
        "https://replicate.delivery/x/out.png", tmp_path / "c", "image",
        web_prefix="/media/c", client=client,
    )
    await client.aclose()
    assert out == "/media/c/image.png"
    assert (tmp_path / "c" / "image.png").read_bytes() == _PNG_BYTES


@pytest.mark.asyncio
async def test_persist_media_http_failure_returns_original(tmp_path):
    client = httpx.AsyncClient(transport=_fail_transport())
    original = "https://replicate.delivery/x/out.png"
    out = await media_store.persist_media(
        original, tmp_path / "c", "image", web_prefix="/media/c", client=client,
    )
    await client.aclose()
    # Falha de download não quebra o run: retorna a uri original, nada gravado.
    assert out == original
    assert not (tmp_path / "c").exists()


# --------------------------------------------------------------------------- #
# persist_media — no-op para uris não-baixáveis                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_persist_media_noop_for_mock_uri(tmp_path):
    uri = "mock://creator/0/base_4k.png"
    out = await media_store.persist_media(
        uri, tmp_path / "c", "image", web_prefix="/media/c",
    )
    assert out == uri
    assert not (tmp_path / "c").exists()


# --------------------------------------------------------------------------- #
# persist_creator_media                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_persist_creator_media_mock_is_noop(tmp_path):
    """Creator mock (mock://, voice-0): nada baixado, dict inalterado, sem disco."""
    creator = {
        "id": "creator-0",
        "angles": ["front"],
        "upscaled_base": "mock://creator/0/base_4k.png",
        "voice_id": "voice-0",
    }
    out = await media_store.persist_creator_media(
        creator, run_id="run-1", media_root=tmp_path,
    )
    assert out["upscaled_base"] == "mock://creator/0/base_4k.png"
    assert out["voice_id"] == "voice-0"
    assert "image_source_uri" not in out
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_persist_creator_media_downloads_image_keeps_voice_id(tmp_path):
    """Image http -> baixada; voice_id (não-URL) -> mantido como referência."""
    creator = {
        "id": "creator-0",
        "angles": ["front"],
        "upscaled_base": "https://replicate.delivery/x/out.png",
        "voice_id": "el_voice_abc",
    }
    client = httpx.AsyncClient(transport=_ok_transport(_PNG_BYTES))
    out = await media_store.persist_creator_media(
        creator, run_id="run-1", media_root=tmp_path, client=client,
    )
    await client.aclose()
    assert out["upscaled_base"] == "/media/run-1/creator-0/image.png"
    assert out["image_source_uri"] == "https://replicate.delivery/x/out.png"
    # voice_id não é URL -> permanece referência, sem source uri
    assert out["voice_id"] == "el_voice_abc"
    assert "voice_source_uri" not in out
    assert (tmp_path / "run-1" / "creator-0" / "image.png").read_bytes() == _PNG_BYTES


@pytest.mark.asyncio
async def test_persist_creator_media_downloads_voice_url(tmp_path):
    """Voice como URL (ex.: ElevenLabs via Replicate) -> baixada como áudio."""
    creator = {
        "id": "creator-2",
        "angles": ["front"],
        "upscaled_base": "https://replicate.delivery/x/out.png",
        "voice_id": "https://replicate.delivery/x/voice.wav",
    }
    client = httpx.AsyncClient(transport=_ok_transport(b"RIFF....WAVE", "audio/wav"))
    out = await media_store.persist_creator_media(
        creator, run_id="run-1", media_root=tmp_path, client=client,
    )
    await client.aclose()
    assert out["voice_id"] == "/media/run-1/creator-2/voice.wav"
    assert out["voice_source_uri"] == "https://replicate.delivery/x/voice.wav"
    assert (tmp_path / "run-1" / "creator-2" / "voice.wav").exists()
