"""Seedance 2.0 final assembly adapter."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.adapters.vercel_seedance_assembly import VercelSeedanceAssemblyAdapter
from orchestrator.adapters import vercel_seedance_assembly as seedance
from orchestrator.graph.state import Artifact, Item


async def test_seedance_assembly_builds_gateway_payload_from_item():
    calls: list[dict[str, Any]] = []

    async def fake_runner(payload: dict[str, Any]) -> bytes:
        calls.append(payload)
        return b"final-mp4"

    adapter = VercelSeedanceAssemblyAdapter(runner=fake_runner)
    item = Item(
        id="item-abc",
        concept={
            "hook": "3 sinais de pele cansada",
            "angle": "problem",
            "offer": "Serum X",
        },
        creator_ref="creator-0",
        creator_image_uri="data:image/png;base64,QUJD",
        script="HOOK: 3 sinais\nBODY: demonstre o serum\nCTA: compre hoje",
        clips=[
            Artifact(
                kind="clip",
                uri="/media/run/items/item-abc/clip-0.mp4",
                meta={"provider": "replicate"},
            )
        ],
    )

    artifact = await adapter.assemble(
        item=item,
        platform="tiktok",
        system_prompt="Final vertical UGC ad. Keep it natural.",
    )

    assert artifact.kind == "video"
    assert artifact.uri == "data:video/mp4;base64," + base64.b64encode(b"final-mp4").decode()
    assert artifact.meta == {
        "provider": "vercel_ai_gateway",
        "model": "bytedance/seedance-2.0",
        "platform": "tiktok",
        "duration": 8,
        "aspect_ratio": "9:16",
        "resolution": "1080x1920",
        "generate_audio": False,
        "cost_usd": 1.344,
        "source_clips": 1,
        "has_reference_image": True,
    }
    assert calls == [
        {
            "model": "bytedance/seedance-2.0",
            "promptText": "Final vertical UGC ad. Keep it natural.",
            "image": {
                "kind": "data_uri",
                "uri": "data:image/png;base64,QUJD",
            },
            "duration": 8,
            "aspectRatio": "9:16",
            "resolution": "1080x1920",
            "generateAudio": False,
            "timeoutMs": 900_000,
        }
    ]


async def test_seedance_assembly_default_prompt_includes_script_and_concept():
    calls: list[dict[str, Any]] = []

    async def fake_runner(payload: dict[str, Any]) -> bytes:
        calls.append(payload)
        return b"mp4"

    adapter = VercelSeedanceAssemblyAdapter(runner=fake_runner)
    item = Item(
        id="item-abc",
        concept={"hook": "hook principal", "offer": "Serum X"},
        creator_ref="creator-0",
        script="HOOK: hook principal\nCTA: testar",
    )

    await adapter.assemble(item=item, platform="instagram")

    prompt = calls[0]["promptText"]
    assert "Final vertical UGC ad for instagram." in prompt
    assert "HOOK: hook principal" in prompt
    assert "hook: hook principal" in prompt
    assert "offer: Serum X" in prompt
    assert "No mock footage." in prompt


async def test_seedance_assembly_prefers_local_creator_image_over_remote_source(tmp_path):
    calls: list[dict[str, Any]] = []

    async def fake_runner(payload: dict[str, Any]) -> bytes:
        calls.append(payload)
        return b"mp4"

    image_path = tmp_path / "creator.png"
    image_path.write_bytes(b"small-png")
    adapter = VercelSeedanceAssemblyAdapter(runner=fake_runner)
    item = Item(
        id="item-abc",
        concept={"hook": "h"},
        creator_ref="creator-0",
        creator_image_uri="https://replicate.delivery/large-upscaled.png",
        creator_image_local_path=str(image_path),
        script="HOOK: h",
    )

    await adapter.assemble(item=item, platform="tiktok")

    assert calls[0]["image"] == {"kind": "path", "path": str(image_path)}


def test_reference_image_payload_uses_compressed_copy_when_local_file_exceeds_limit(
    tmp_path, monkeypatch
):
    large = tmp_path / "large.png"
    compressed = tmp_path / "large-seedance.webp"
    large.write_bytes(b"x" * 11)

    def fake_compress(path, *, max_bytes):
        assert path == large
        assert max_bytes == 10
        compressed.write_bytes(b"small")
        return compressed

    monkeypatch.setattr(seedance, "_compress_image_for_gateway", fake_compress)

    payload = seedance._reference_image_payload(str(large), max_bytes=10)

    assert payload == {"kind": "path", "path": str(compressed)}


async def test_seedance_assembly_downloads_and_compresses_remote_reference_when_no_local_path(
    tmp_path, monkeypatch
):
    calls: list[dict[str, Any]] = []
    remote = tmp_path / "remote.png"
    compressed = tmp_path / "remote-seedance.jpg"

    async def fake_runner(payload: dict[str, Any]) -> bytes:
        calls.append(payload)
        return b"mp4"

    async def fake_download(uri: str) -> Path:
        assert uri == "https://replicate.delivery/large-upscaled.png"
        remote.write_bytes(b"x" * (seedance.GATEWAY_IMAGE_TARGET_BYTES + 1))
        return remote

    def fake_compress(path, *, max_bytes):
        assert path == remote
        compressed.write_bytes(b"small")
        return compressed

    monkeypatch.setattr(seedance, "_download_reference_image", fake_download)
    monkeypatch.setattr(seedance, "_compress_image_for_gateway", fake_compress)

    adapter = VercelSeedanceAssemblyAdapter(runner=fake_runner)
    item = Item(
        id="item-abc",
        concept={"hook": "h"},
        creator_ref="creator-0",
        creator_image_uri="https://replicate.delivery/large-upscaled.png",
        script="HOOK: h",
    )

    await adapter.assemble(item=item, platform="tiktok")

    assert calls[0]["image"] == {"kind": "path", "path": str(compressed)}


# ------------------------------------------------------------------ #
# build_vercel_seedance_assembly_adapter / _tier                     #
# ------------------------------------------------------------------ #

def test_build_adapter_falls_back_to_default_cost_when_no_seedance_tier():
    adapter = seedance.build_vercel_seedance_assembly_adapter({"tiers": []})
    assert adapter.cost_per_second == seedance.DEFAULT_COST_PER_SECOND


def test_build_adapter_reads_seedance_tier_cost():
    adapter = seedance.build_vercel_seedance_assembly_adapter(
        {"tiers": [{"name": "seedance", "cost_per_second": 0.25}]}
    )
    assert adapter.cost_per_second == 0.25


def test_tier_returns_empty_when_absent():
    assert seedance._tier({"tiers": [{"name": "ltx"}]}, "seedance") == {}


# ------------------------------------------------------------------ #
# _reference_image_payload                                           #
# ------------------------------------------------------------------ #

def test_reference_image_payload_none_returns_none():
    assert seedance._reference_image_payload(None) is None


def test_reference_image_payload_data_uri():
    assert seedance._reference_image_payload("data:image/png;base64,QUJD") == {
        "kind": "data_uri",
        "uri": "data:image/png;base64,QUJD",
    }


def test_reference_image_payload_http_url():
    assert seedance._reference_image_payload("https://cdn.example/x.png") == {
        "kind": "url",
        "uri": "https://cdn.example/x.png",
    }


def test_reference_image_payload_local_below_limit_passes_through(tmp_path):
    p = tmp_path / "small.png"
    p.write_bytes(b"tiny")
    assert seedance._reference_image_payload(str(p)) == {"kind": "path", "path": str(p)}


async def test_prepare_reference_image_payload_none_returns_none():
    assert await seedance._prepare_reference_image_payload(None) is None


# ------------------------------------------------------------------ #
# _download_reference_image (httpx via monkeypatch, sem rede)         #
# ------------------------------------------------------------------ #

class _FakeResponse:
    def __init__(self, content: bytes, *, status_ok: bool = True):
        self.content = content
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, uri: str):
        return self._response


async def test_download_reference_image_writes_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        seedance.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(_FakeResponse(b"IMG")),
    )
    path = await seedance._download_reference_image("https://cdn.example/pic.png")
    try:
        assert path.read_bytes() == b"IMG"
        assert path.suffix == ".png"
    finally:
        path.unlink(missing_ok=True)


async def test_download_reference_image_cleans_up_on_http_error(monkeypatch):
    captured: dict[str, Path] = {}
    real_named = seedance.tempfile.NamedTemporaryFile

    def spy_named(*a, **kw):
        handle = real_named(*a, **kw)
        captured["path"] = Path(handle.name)
        return handle

    monkeypatch.setattr(seedance.tempfile, "NamedTemporaryFile", spy_named)
    monkeypatch.setattr(
        seedance.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(_FakeResponse(b"", status_ok=False)),
    )

    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        await seedance._download_reference_image("https://cdn.example/pic.png")
    assert not captured["path"].exists()


# ------------------------------------------------------------------ #
# _local_path_for_reference                                          #
# ------------------------------------------------------------------ #

def test_local_path_for_reference_media_prefix():
    path = seedance._local_path_for_reference("/media/run/items/item-abc/clip-0.png")
    assert str(path).endswith(".orchestrator/media/run/items/item-abc/clip-0.png")


def test_local_path_for_reference_plain_path():
    assert seedance._local_path_for_reference("/abs/pic.png") == Path("/abs/pic.png")


# ------------------------------------------------------------------ #
# _compress_image_for_gateway (Pillow real)                          #
# ------------------------------------------------------------------ #

def _write_big_png(path: Path) -> None:
    from PIL import Image

    # Gradiente suave em RGBA: acima de 2048px (força thumbnail), modo != RGB
    # (força convert), mas compressível o suficiente para caber no limite de teste.
    img = Image.new("RGBA", (2600, 2600))
    px = img.load()
    for y in range(2600):
        row = (y * 255) // 2600
        for x in range(2600):
            px[x, y] = ((x * 255) // 2600, row, (x + y) % 256, 255)
    img.save(path, format="PNG")


def test_compress_image_for_gateway_produces_small_jpeg(tmp_path):
    src = tmp_path / "big.png"
    _write_big_png(src)
    limit = 400_000
    out = seedance._compress_image_for_gateway(src, max_bytes=limit)
    try:
        assert out.suffix == ".jpg"
        assert out.stat().st_size <= limit
        # thumbnail respeita a dimensão máxima do gateway
        from PIL import Image

        with Image.open(out) as im:
            assert max(im.size) <= seedance.GATEWAY_IMAGE_MAX_DIMENSION
    finally:
        out.unlink(missing_ok=True)


def test_compress_image_for_gateway_raises_when_cannot_fit(tmp_path):
    src = tmp_path / "big.png"
    _write_big_png(src)
    with pytest.raises(RuntimeError, match="não foi possível compactar"):
        seedance._compress_image_for_gateway(src, max_bytes=10)


# ------------------------------------------------------------------ #
# _run_node_bridge (subprocess via monkeypatch, sem node real)       #
# ------------------------------------------------------------------ #

class _FakeProc:
    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, _input: bytes):
        return self._stdout, self._stderr


def _patch_subprocess(monkeypatch, proc: _FakeProc, *, sink: dict | None = None):
    async def fake_exec(*args, **kwargs):
        if sink is not None:
            sink["args"] = args
        return proc

    monkeypatch.setattr(seedance.asyncio, "create_subprocess_exec", fake_exec)


async def test_run_node_bridge_success_reads_output_file(monkeypatch, tmp_path):
    out_file = tmp_path / "out.mp4"
    out_file.write_bytes(b"FINAL-MP4")
    monkeypatch.setattr(seedance, "_temp_output_path", lambda: out_file)
    _patch_subprocess(monkeypatch, _FakeProc(stdout=json.dumps({"ok": True}).encode()))

    data = await seedance._run_node_bridge({"model": "m", "promptText": "p"})

    assert data == b"FINAL-MP4"
    assert not out_file.exists()  # limpo no finally


async def test_run_node_bridge_non_json_stdout_raises(monkeypatch, tmp_path):
    out_file = tmp_path / "out.mp4"
    out_file.write_bytes(b"x")
    monkeypatch.setattr(seedance, "_temp_output_path", lambda: out_file)
    _patch_subprocess(monkeypatch, _FakeProc(stdout=b"not json", stderr=b"trace"))

    with pytest.raises(RuntimeError, match="non-JSON stdout"):
        await seedance._run_node_bridge({"model": "m"})
    assert not out_file.exists()


async def test_run_node_bridge_failure_returncode_raises(monkeypatch, tmp_path):
    out_file = tmp_path / "out.mp4"
    out_file.write_bytes(b"x")
    monkeypatch.setattr(seedance, "_temp_output_path", lambda: out_file)
    _patch_subprocess(
        monkeypatch,
        _FakeProc(stdout=json.dumps({"ok": False, "error": "gateway 500"}).encode(), returncode=1),
    )

    with pytest.raises(RuntimeError, match="gateway 500"):
        await seedance._run_node_bridge({"model": "m"})
    assert not out_file.exists()


def test_temp_output_path_is_mp4(tmp_path):
    path = seedance._temp_output_path()
    try:
        assert path.exists()
        assert path.suffix == ".mp4"
    finally:
        path.unlink(missing_ok=True)
