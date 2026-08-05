from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from orchestrator.adapters.base import RenderedMedia
from orchestrator.adapters.ffmpeg_assembly import (
    FfmpegAssemblyAdapter,
    build_ffmpeg_assembly_adapter,
)
from orchestrator.graph.state import Artifact, Item


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def _video(path, color: str) -> None:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=90x160:d=1:r=24",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
    )


def _voice(path, duration: float) -> None:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:a",
        "libmp3lame",
        str(path),
    )


async def test_ffmpeg_assembly_concatenates_two_clips_and_muxes_aac_voiceover(
    tmp_path,
):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    voice = tmp_path / "voice.mp3"
    _video(first, "red")
    _video(second, "blue")
    _voice(voice, 1.8)
    item = Item(
        id="item-1",
        concept={"hook": "h"},
        clips=[
            Artifact(kind="clip", uri=str(first)),
            Artifact(kind="clip", uri=str(second)),
        ],
        voiceover=Artifact(kind="voiceover", uri=str(voice)),
    )
    adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
        width=90,
        height=160,
        fps=24,
        audio_speedup_max=1.10,
    )

    rendered = await asyncio.wait_for(
        adapter.assemble(item, platform="tiktok"),
        timeout=30,
    )

    assert isinstance(rendered, RenderedMedia)
    assert rendered.content_type == "video/mp4"
    assert b"ftyp" in rendered.data[:64]
    assert rendered.meta["provider"] == "ffmpeg"
    assert rendered.meta["video_codec"] == "h264"
    assert rendered.meta["audio_codec"] == "aac"
    assert rendered.meta["source_clips"] == 2
    assert rendered.meta["cost_usd"] == 0.0
    final_path = tmp_path / "final.mp4"
    final_path.write_bytes(rendered.data)
    second_half_pixel = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "1.5",
            "-i",
            str(final_path),
            "-vf",
            "scale=1:1",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    red, _green, blue = second_half_pixel[:3]
    assert blue > red


async def test_ffmpeg_assembly_rejects_voiceover_that_needs_more_than_ten_percent_speedup(
    tmp_path,
):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    voice = tmp_path / "voice.mp3"
    _video(first, "red")
    _video(second, "blue")
    _voice(voice, 2.4)
    item = Item(
        id="item-1",
        concept={"hook": "h"},
        clips=[
            Artifact(kind="clip", uri=str(first)),
            Artifact(kind="clip", uri=str(second)),
        ],
        voiceover=Artifact(kind="voiceover", uri=str(voice)),
    )
    adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
        width=90,
        height=160,
        audio_speedup_max=1.10,
    )

    with pytest.raises(RuntimeError, match="voiceover is too long"):
        await adapter.assemble(item, platform="tiktok")


def test_ffmpeg_assembly_validates_duration_and_speed_configuration():
    with pytest.raises(ValueError, match="two clip durations"):
        FfmpegAssemblyAdapter(
            final_duration_seconds=3,
            clip_duration_seconds=1,
        )
    with pytest.raises(ValueError, match="audio_speedup_max"):
        FfmpegAssemblyAdapter(audio_speedup_max=0.9)


async def test_ffmpeg_run_surfaces_missing_binary_failure_and_timeout():
    adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
    )
    with pytest.raises(RuntimeError, match="binary is missing"):
        await adapter._run("/definitely/missing/ffmpeg")
    with pytest.raises(RuntimeError, match="failed: expected-error"):
        await adapter._run(
            "/bin/sh",
            "-c",
            "printf expected-error >&2; exit 2",
        )
    timeout_adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
        timeout_seconds=0.01,
    )
    with pytest.raises(RuntimeError, match="timed out"):
        await timeout_adapter._run("/bin/sh", "-c", "sleep 1")


async def test_ffmpeg_materializes_data_file_http_and_rejects_missing(
    monkeypatch,
    tmp_path,
):
    adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
    )
    data_dest = tmp_path / "data.bin"
    await adapter._materialize("data:audio/mpeg;base64,SUQz", data_dest)
    assert data_dest.read_bytes() == b"ID3"

    source = tmp_path / "source clip.mp4"
    source.write_bytes(b"clip")
    file_dest = tmp_path / "file.bin"
    await adapter._materialize(source.as_uri(), file_dest)
    assert file_dest.read_bytes() == b"clip"

    class _Response:
        content = b"remote"

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, uri):
            assert uri == "https://cdn.example/media.mp4"
            return _Response()

    monkeypatch.setattr(
        "orchestrator.adapters.ffmpeg_assembly.httpx.AsyncClient",
        _Client,
    )
    http_dest = tmp_path / "http.bin"
    await adapter._materialize(
        "https://cdn.example/media.mp4",
        http_dest,
    )
    assert http_dest.read_bytes() == b"remote"

    with pytest.raises(RuntimeError, match="not readable"):
        await adapter._materialize(
            str(tmp_path / "missing.mp4"),
            tmp_path / "missing.bin",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "did not report"),
        (b'{"format":{"duration":"0"}}', "positive"),
    ],
)
async def test_ffmpeg_duration_rejects_invalid_probe_output(
    monkeypatch,
    tmp_path,
    payload,
    message,
):
    adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
    )

    async def probe(*args):
        return payload, b""

    monkeypatch.setattr(adapter, "_run", probe)
    with pytest.raises(RuntimeError, match=message):
        await adapter._duration(tmp_path / "voice.mp3")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "invalid output JSON"),
        (
            {"streams": [], "format": {"duration": "2"}},
            "H.264",
        ),
        (
            {
                "streams": [{"codec_type": "video", "codec_name": "h264"}],
                "format": {"duration": "2"},
            },
            "AAC",
        ),
        (
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"duration": "0"},
            },
            "invalid duration",
        ),
    ],
)
async def test_ffmpeg_validation_rejects_missing_required_output_contract(
    monkeypatch,
    tmp_path,
    payload,
    message,
):
    adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
    )
    encoded = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload).encode()
    )

    async def probe(*args):
        return encoded, b""

    monkeypatch.setattr(adapter, "_run", probe)
    with pytest.raises(RuntimeError, match=message):
        await adapter._validate_output(tmp_path / "final.mp4")


async def test_ffmpeg_assembly_requires_two_clips_and_voiceover():
    adapter = FfmpegAssemblyAdapter(
        final_duration_seconds=2,
        clip_duration_seconds=1,
    )
    with pytest.raises(RuntimeError, match="two approved clips"):
        await adapter.assemble(
            Item(
                concept={},
                clips=[Artifact(kind="clip", uri="mock://one")],
            ),
            platform="tiktok",
        )
    with pytest.raises(RuntimeError, match="approved voiceover"):
        await adapter.assemble(
            Item(
                concept={},
                clips=[
                    Artifact(kind="clip", uri="mock://one"),
                    Artifact(kind="clip", uri="mock://two"),
                ],
            ),
            platform="tiktok",
        )


def test_ffmpeg_factory_supports_legacy_resolution_label_and_defaults():
    adapter = build_ffmpeg_assembly_adapter(
        {
            "clip": {"duration_seconds": 3, "fps": 30},
            "assembly": {"resolution": "1080p"},
        }
    )

    assert adapter.final_duration_seconds == 6
    assert adapter.clip_duration_seconds == 3
    assert (adapter.width, adapter.height, adapter.fps) == (1080, 1920, 30)
