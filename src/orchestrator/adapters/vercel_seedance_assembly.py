"""Assembly final via Seedance 2.0 no Vercel AI Gateway.

O runtime Python continua orquestrando o LangGraph. A geração de vídeo passa por
um bridge Node pequeno porque o AI SDK expõe `experimental_generateVideo` em JS.
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import httpx

from orchestrator.graph.state import Artifact, Item
from orchestrator.tracing import traced

BridgeRunner = Callable[[dict[str, Any]], Awaitable[bytes]]

DEFAULT_MODEL = "bytedance/seedance-2.0"
DEFAULT_DURATION = 8
DEFAULT_ASPECT_RATIO = "9:16"
DEFAULT_RESOLUTION = "1080x1920"
DEFAULT_TIMEOUT_MS = 900_000
DEFAULT_COST_PER_SECOND = 0.168
GATEWAY_IMAGE_LIMIT_BYTES = 30 * 1024 * 1024
GATEWAY_IMAGE_TARGET_BYTES = 28 * 1024 * 1024
GATEWAY_IMAGE_MAX_DIMENSION = 2048


class VercelSeedanceAssemblyAdapter:
    """Gera o vídeo final a partir do briefing completo do item."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        duration: int = DEFAULT_DURATION,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        generate_audio: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        cost_per_second: float = DEFAULT_COST_PER_SECOND,
        runner: Optional[BridgeRunner] = None,
    ) -> None:
        self.model = model
        self.duration = duration
        self.aspect_ratio = aspect_ratio
        self.resolution = resolution
        self.generate_audio = generate_audio
        self.timeout_ms = timeout_ms
        self.cost_per_second = cost_per_second
        self._runner = runner or _run_node_bridge

    @traced("adapter.vercel_seedance_assembly.assemble", run_type="tool", step=8, provider="vercel_ai_gateway")
    async def assemble(
        self,
        item: Item,
        platform: str,
        system_prompt: Optional[str] = None,
    ) -> Artifact:
        prompt = system_prompt or build_default_prompt(item, platform)
        payload: dict[str, Any] = {
            "model": self.model,
            "promptText": prompt,
            "duration": self.duration,
            "aspectRatio": self.aspect_ratio,
            "resolution": self.resolution,
            "generateAudio": self.generate_audio,
            "timeoutMs": self.timeout_ms,
        }
        cleanup_paths: list[Path] = []
        image = await _prepare_reference_image_payload(
            item.creator_image_local_path or item.creator_image_uri,
            cleanup_paths=cleanup_paths,
        )
        if image is not None:
            payload["image"] = image

        try:
            data = await self._runner(payload)
        finally:
            for path in cleanup_paths:
                path.unlink(missing_ok=True)
        return Artifact(
            kind="video",
            uri="data:video/mp4;base64," + base64.b64encode(data).decode(),
            meta={
                "provider": "vercel_ai_gateway",
                "model": self.model,
                "platform": platform,
                "duration": self.duration,
                "aspect_ratio": self.aspect_ratio,
                "resolution": self.resolution,
                "generate_audio": self.generate_audio,
                "cost_usd": round(self.cost_per_second * self.duration, 4),
                "source_clips": len(item.clips or []),
                "has_reference_image": image is not None,
            },
        )


def build_default_prompt(item: Item, platform: str) -> str:
    parts = [
        f"Final vertical UGC ad for {platform}.",
        "Use the creator reference image as the consistent on-camera creator.",
        "Create one polished final video from the approved concept and script.",
        "No mock footage. No placeholder frames. No captions burned into the video.",
    ]
    if item.script:
        parts.append(f"Script:\n{item.script}")

    concept = item.concept or {}
    concept_bits = [
        f"{key}: {concept[key]}"
        for key in ("hook", "angle", "hook_style", "offer", "format")
        if concept.get(key)
    ]
    if concept_bits:
        parts.append("Concept context: " + "; ".join(concept_bits))
    if item.creator_ref:
        parts.append(f"Creator ref: {item.creator_ref}")
    return "\n\n".join(parts)


def build_vercel_seedance_assembly_adapter(
    pipeline: dict[str, Any],
) -> VercelSeedanceAssemblyAdapter:
    assembly_cfg = pipeline.get("assembly", {})
    clip_cfg = pipeline.get("clip", {})
    seedance = _tier(pipeline, "seedance")
    return VercelSeedanceAssemblyAdapter(
        model=str(assembly_cfg.get("model", DEFAULT_MODEL)),
        duration=int(assembly_cfg.get("duration_seconds", clip_cfg.get("duration_seconds", 8))),
        aspect_ratio=str(assembly_cfg.get("aspect_ratio", clip_cfg.get("aspect_ratio", "9:16"))),
        resolution=str(assembly_cfg.get("resolution", DEFAULT_RESOLUTION)),
        generate_audio=bool(assembly_cfg.get("generate_audio", False)),
        timeout_ms=int(assembly_cfg.get("timeout_ms", DEFAULT_TIMEOUT_MS)),
        cost_per_second=float(
            assembly_cfg.get("cost_per_second", seedance.get("cost_per_second", DEFAULT_COST_PER_SECOND))
        ),
    )


def _tier(pipeline: dict[str, Any], name: str) -> dict[str, Any]:
    for tier in pipeline.get("tiers", []):
        if tier.get("name") == name:
            return tier
    return {}


def _reference_image_payload(
    uri: Optional[str],
    *,
    max_bytes: int = GATEWAY_IMAGE_TARGET_BYTES,
    cleanup_paths: Optional[list[Path]] = None,
) -> Optional[dict[str, str]]:
    if not uri:
        return None
    if uri.startswith("data:"):
        return {"kind": "data_uri", "uri": uri}
    if uri.startswith(("http://", "https://")):
        return {"kind": "url", "uri": uri}
    path = _local_path_for_reference(uri)
    if path.exists() and path.stat().st_size > max_bytes:
        path = _compress_image_for_gateway(path, max_bytes=max_bytes)
        if cleanup_paths is not None:
            cleanup_paths.append(path)
    return {"kind": "path", "path": str(path)}


async def _prepare_reference_image_payload(
    uri: Optional[str],
    *,
    cleanup_paths: Optional[list[Path]] = None,
) -> Optional[dict[str, str]]:
    if not uri:
        return None
    if uri.startswith(("http://", "https://")):
        path = await _download_reference_image(uri)
        if cleanup_paths is not None:
            cleanup_paths.append(path)
        return _reference_image_payload(str(path), cleanup_paths=cleanup_paths)
    return _reference_image_payload(uri, cleanup_paths=cleanup_paths)


async def _download_reference_image(uri: str) -> Path:
    suffix = Path(urlparse(uri).path).suffix or ".img"
    handle = tempfile.NamedTemporaryFile(
        prefix="seedance-source-", suffix=suffix, delete=False
    )
    path = Path(handle.name)
    handle.close()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(uri)
            response.raise_for_status()
            path.write_bytes(response.content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _local_path_for_reference(uri: str) -> Path:
    """Converte path web `/media/...` em path de disco; outros paths passam direto."""
    if uri.startswith("/media/"):
        root = Path(__file__).resolve().parents[3]
        return root / ".orchestrator" / "media" / uri.removeprefix("/media/")
    return Path(uri)


def _compress_image_for_gateway(
    path: Path,
    *,
    max_bytes: int = GATEWAY_IMAGE_TARGET_BYTES,
) -> Path:
    """Cria uma cópia JPEG menor que o limite do Vercel Gateway para imagem input."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - depende do ambiente de instalação
        raise RuntimeError(
            "Pillow é necessário para compactar imagens antes do Seedance. "
            "Instale as dependências do projeto novamente."
        ) from exc

    handle = tempfile.NamedTemporaryFile(
        prefix="seedance-ref-", suffix=".jpg", delete=False
    )
    handle.close()
    out = Path(handle.name)

    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail(
            (GATEWAY_IMAGE_MAX_DIMENSION, GATEWAY_IMAGE_MAX_DIMENSION),
            Image.Resampling.LANCZOS,
        )
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        for quality in (88, 78, 68, 58, 48, 38):
            image.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
            if out.stat().st_size <= max_bytes:
                return out

    out.unlink(missing_ok=True)
    raise RuntimeError(
        f"não foi possível compactar {path} para menos de {max_bytes} bytes"
    )


async def _run_node_bridge(payload: dict[str, Any]) -> bytes:
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "vercel_generate_video.mjs"
    output_path = _temp_output_path()
    full_payload = {**payload, "outputPath": str(output_path)}

    proc = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(root),
    )
    stdout, stderr = await proc.communicate(json.dumps(full_payload).encode())
    try:
        body = json.loads(stdout.decode() or "{}")
    except json.JSONDecodeError as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            "Seedance bridge returned non-JSON stdout: "
            f"{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}"
        ) from exc

    if proc.returncode != 0 or not body.get("ok"):
        output_path.unlink(missing_ok=True)
        message = body.get("error") or stderr.decode(errors="replace") or "unknown bridge error"
        raise RuntimeError(f"Seedance bridge failed: {message}")

    try:
        return output_path.read_bytes()
    finally:
        output_path.unlink(missing_ok=True)


def _temp_output_path() -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="seedance-", suffix=".mp4", delete=False)
    handle.close()
    return Path(handle.name)
