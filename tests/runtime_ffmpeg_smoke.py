"""Behavioral smoke test for the FFmpeg shipped in the runtime image."""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from orchestrator.adapters.ffmpeg_assembly import FfmpegAssemblyAdapter
from orchestrator.graph.state import Artifact, Item


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def _video(path: Path, color: str) -> None:
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


def _voice(path: Path) -> None:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1.8",
        "-c:a",
        "libmp3lame",
        str(path),
    )


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


async def _assemble_mono_voiceover() -> None:
    with tempfile.TemporaryDirectory(prefix="ffmpeg-runtime-smoke-") as raw_tmp:
        work = Path(raw_tmp)
        first = work / "first.mp4"
        second = work / "second.mp4"
        voice = work / "voice.mp3"
        final = work / "final.mp4"
        _video(first, "red")
        _video(second, "blue")
        _voice(voice)

        item = Item(
            id="runtime-smoke",
            concept={"hook": "runtime compatibility"},
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

        rendered = await adapter.assemble(item, platform="tiktok")
        final.write_bytes(rendered.data)
        payload = _probe(final)
        streams = payload["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")

        assert video["codec_name"] == "h264"
        assert audio["codec_name"] == "aac"
        assert audio["sample_rate"] == "48000"
        assert audio["channels"] == 1
        assert audio["channel_layout"] == "mono"
        assert 1.9 <= float(payload["format"]["duration"]) <= 2.1


if __name__ == "__main__":
    asyncio.run(_assemble_mono_voiceover())
