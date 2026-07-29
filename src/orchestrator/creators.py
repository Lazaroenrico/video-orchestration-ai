"""Vocabulário comum dos stores de creators."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


_AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg")


def _is_playable_voice_uri(uri: Any) -> bool:
    if not isinstance(uri, str) or not uri:
        return False
    lower = uri.lower()
    if lower.startswith("data:audio/"):
        return True
    parsed = urlparse(uri)
    if parsed.scheme in {"http", "https", "r2"}:
        return parsed.path.lower().endswith(_AUDIO_EXTENSIONS)
    if parsed.scheme:
        return False
    if uri.startswith(("/", "./", "../")):
        return parsed.path.lower().endswith(_AUDIO_EXTENSIONS)
    return False


def normalize_creator_fields(creator: dict[str, Any]) -> dict[str, Any]:
    """Normaliza mídia e preserva aliases legados ``image``/``voice``."""
    image_uri = (
        creator.get("image_uri")
        or creator.get("image")
        or creator.get("upscaled_base")
    )
    voice_ref = (
        creator.get("voice_model_ref")
        or creator.get("voice_ref")
        or creator.get("voice")
        or creator.get("voice_id")
    )
    voice_preview_uri = (
        creator.get("voice_preview_uri")
        or creator.get("voice_preview")
        or creator.get("preview_uri")
    )
    if voice_preview_uri is None and _is_playable_voice_uri(voice_ref):
        voice_preview_uri = voice_ref
    return {
        "image_uri": image_uri,
        "voice_ref": voice_ref,
        "voice_preview_uri": voice_preview_uri,
        "image": image_uri,
        "voice": voice_ref,
        "angles": list(creator.get("angles") or []),
        "voice_reroll_count": creator.get("voice_reroll_count"),
    }


def normalize_creator_payload(creator: dict[str, Any]) -> dict[str, Any]:
    """Project an internal creator into the public HTTP/SSE media contract."""
    fields = normalize_creator_fields(creator)
    normalized = {
        "id": creator.get("id") or creator.get("creator_id"),
        "image_uri": fields["image_uri"],
        "voice_ref": fields["voice_ref"],
        "voice_preview_uri": fields["voice_preview_uri"],
        "image": fields["image"],
        "voice": fields["voice"],
        "angles": fields["angles"],
    }
    for key in (
        "archetype",
        "visual_brief",
        "voice_brief",
        "performance_style",
        "exclusions",
    ):
        if key in creator:
            normalized[key] = creator[key]
    return normalized
