"""Seedance 2.0 final assembly adapter."""
from __future__ import annotations

import base64
from typing import Any

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
