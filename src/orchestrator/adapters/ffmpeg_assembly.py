"""Deterministic final assembly using local FFmpeg/ffprobe binaries."""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import httpx

from orchestrator.adapters.base import RenderedMedia
from orchestrator.graph.state import Item
from orchestrator.storage.base import decode_data_uri
from orchestrator.tracing import traced


class FfmpegAssemblyAdapter:
    """Concatenate two silent clips and mux the approved narration.

    The renderer never trims spoken words. Narration may be accelerated by at
    most ``audio_speedup_max``; longer input is rejected explicitly.
    """

    def __init__(
        self,
        *,
        final_duration_seconds: float = 16,
        clip_duration_seconds: float = 8,
        width: int = 1080,
        height: int = 1920,
        fps: int = 24,
        audio_speedup_max: float = 1.10,
        timeout_seconds: float = 300,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
    ) -> None:
        self.final_duration_seconds = float(final_duration_seconds)
        self.clip_duration_seconds = float(clip_duration_seconds)
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.audio_speedup_max = float(audio_speedup_max)
        self.timeout_seconds = float(timeout_seconds)
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        if not math.isclose(
            self.final_duration_seconds,
            self.clip_duration_seconds * 2,
            rel_tol=0,
            abs_tol=0.001,
        ):
            raise ValueError("final duration must equal two clip durations")
        if self.audio_speedup_max < 1 or self.audio_speedup_max > 2:
            raise ValueError("audio_speedup_max must be between 1 and 2")

    @staticmethod
    async def _communicate(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """Drain both pipes without the late ``Process.wait()`` race in communicate()."""
        assert process.stdout is not None
        assert process.stderr is not None
        stdout, stderr = await asyncio.gather(
            process.stdout.read(),
            process.stderr.read(),
        )
        while process.returncode is None:
            await asyncio.sleep(0)
        await process.wait()
        return stdout, stderr

    async def _run(self, *command: str) -> tuple[bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"required media binary is missing: {command[0]}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                self._communicate(process),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            await self._communicate(process)
            raise RuntimeError(f"{command[0]} timed out") from exc
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{command[0]} failed: {detail[-2000:]}")
        return stdout, stderr

    async def _materialize(self, uri: str, destination: Path) -> None:
        if uri.startswith("data:"):
            data, _ = decode_data_uri(uri)
            destination.write_bytes(data)
            return
        parsed = urlparse(uri)
        if parsed.scheme in {"http", "https"}:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                response = await client.get(uri)
                response.raise_for_status()
                destination.write_bytes(response.content)
            return
        source = (
            Path(unquote(parsed.path))
            if parsed.scheme == "file"
            else Path(uri)
        )
        if not source.is_file():
            raise RuntimeError(f"assembly input is not readable: {uri}")
        destination.write_bytes(source.read_bytes())

    async def _duration(self, path: Path) -> float:
        stdout, _ = await self._run(
            self.ffprobe_binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        )
        try:
            duration = float(json.loads(stdout)["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("ffprobe did not report voiceover duration") from exc
        if duration <= 0:
            raise RuntimeError("voiceover duration must be positive")
        return duration

    async def _validate_output(self, path: Path) -> dict[str, Any]:
        stdout, _ = await self._run(
            self.ffprobe_binary,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ffprobe returned invalid output JSON") from exc
        streams = payload.get("streams") or []
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        if video is None or video.get("codec_name") != "h264":
            raise RuntimeError("assembled output is missing an H.264 video stream")
        if audio is None or audio.get("codec_name") != "aac":
            raise RuntimeError("assembled output is missing an AAC audio stream")
        duration = float((payload.get("format") or {}).get("duration") or 0)
        if duration <= 0:
            raise RuntimeError("assembled output has invalid duration")
        return {
            "video_codec": "h264",
            "audio_codec": "aac",
            "duration_seconds": round(duration, 3),
        }

    @traced(
        "adapter.ffmpeg_assembly.assemble",
        run_type="tool",
        step=8,
        provider="ffmpeg",
    )
    async def assemble(
        self,
        item: Item,
        platform: str,
        system_prompt: Optional[str] = None,
    ) -> RenderedMedia:
        if len(item.clips) < 2:
            raise RuntimeError("FFmpeg assembly requires two approved clips")
        if item.voiceover is None:
            raise RuntimeError("FFmpeg assembly requires an approved voiceover")
        selected_clips = item.clips[-2:]

        with tempfile.TemporaryDirectory(prefix="orchestrator-assembly-") as raw_tmp:
            work = Path(raw_tmp)
            clip_a = work / "clip-a.mp4"
            clip_b = work / "clip-b.mp4"
            voice = work / "voiceover.audio"
            output = work / "assembled.mp4"
            await asyncio.gather(
                self._materialize(selected_clips[0].uri, clip_a),
                self._materialize(selected_clips[1].uri, clip_b),
                self._materialize(item.voiceover.uri, voice),
            )
            voice_duration = await self._duration(voice)
            effective_final_duration = max(self.final_duration_seconds, voice_duration)
            per_clip_duration = effective_final_duration / 2.0
            speed = 1.0

            scene_name, acoustic_echo = _detect_acoustic_scene(item, system_prompt)

            clip_duration_str = f"{per_clip_duration:g}"
            final_duration_str = f"{effective_final_duration:g}"
            video_filter = (
                "setpts=PTS-STARTPTS,"
                f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
                f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"fps={self.fps},format=yuv420p,"
                f"tpad=stop_mode=clone:stop_duration={clip_duration_str},"
                f"trim=duration={clip_duration_str},setpts=PTS-STARTPTS"
            )
            audio_filter = (
                "atempo=1.0,"
                f"{acoustic_echo},"
                "loudnorm=I=-16:LRA=11:TP=-1.5,"
                "aresample=48000:out_chlayout=mono,"
                f"apad,atrim=duration={final_duration_str}"
            )
            filter_complex = (
                f"[0:v]{video_filter}[v0];"
                f"[1:v]{video_filter}[v1];"
                "[v0][v1]concat=n=2:v=1:a=0[v];"
                f"[2:a]{audio_filter}[a]"
            )
            await self._run(
                self.ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(clip_a),
                "-i",
                str(clip_b),
                "-i",
                str(voice),
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-t",
                final_duration_str,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output),
            )
            validated = await self._validate_output(output)
            return RenderedMedia(
                data=output.read_bytes(),
                content_type="video/mp4",
                meta={
                    "provider": "ffmpeg",
                    "platform": platform,
                    "source_clips": 2,
                    "voiceover_speed": round(speed, 6),
                    "acoustic_scene": scene_name,
                    "cost_usd": 0.0,
                    **validated,
                },
            )


def _detect_acoustic_scene(item: Item, system_prompt: Optional[str] = None) -> tuple[str, str]:
    """Retorna a cena identificada e a cadeia de filtro de acústica ambiental no FFmpeg."""
    text = " ".join([
        str(item.concept.get("hook") or ""),
        str(item.concept.get("angle") or ""),
        str(item.concept.get("format") or ""),
        str(system_prompt or ""),
    ]).casefold()

    if any(k in text for k in ("bathroom", "banheiro", "azulejo", "tile")):
        return "bathroom", "aecho=0.8:0.88:32:0.4"
    if any(k in text for k in ("bedroom", "quarto", "closet", "bed")):
        return "bedroom", "aecho=0.8:0.25:10:0.1"
    if any(k in text for k in ("outdoor", "rua", "street", "park", "praça")):
        return "outdoor", "highpass=f=80,lowpass=f=12000"
    return "default", "aecho=0.8:0.35:15:0.15"


def build_ffmpeg_assembly_adapter(
    pipeline: dict[str, Any],
) -> FfmpegAssemblyAdapter:
    assembly = pipeline.get("assembly") or {}
    clip = pipeline.get("clip") or {}
    resolution = str(assembly.get("resolution") or "1080x1920")
    if "x" in resolution:
        width_raw, height_raw = resolution.casefold().split("x", 1)
        width, height = int(width_raw), int(height_raw)
    else:
        width, height = 1080, 1920
    return FfmpegAssemblyAdapter(
        final_duration_seconds=float(
            assembly.get(
                "final_duration_seconds",
                float(clip.get("duration_seconds", 8)) * 2,
            )
        ),
        clip_duration_seconds=float(clip.get("duration_seconds", 8)),
        width=width,
        height=height,
        fps=int(assembly.get("fps", clip.get("fps", 24))),
        audio_speedup_max=float(assembly.get("audio_speedup_max", 1.10)),
        timeout_seconds=float(assembly.get("timeout_seconds", 300)),
    )
