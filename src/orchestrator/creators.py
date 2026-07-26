"""Vocabulário comum dos stores de creators."""
from __future__ import annotations

from typing import Any


def normalize_creator_fields(creator: dict[str, Any]) -> dict[str, Any]:
    """Normaliza mídia e preserva aliases legados ``image``/``voice``."""
    image_uri = (
        creator.get("image_uri")
        or creator.get("image")
        or creator.get("upscaled_base")
    )
    voice_ref = (
        creator.get("voice_ref")
        or creator.get("voice")
        or creator.get("voice_id")
    )
    voice_preview_uri = (
        creator.get("voice_preview_uri")
        or creator.get("voice_preview")
        or creator.get("preview_uri")
    )
    return {
        "image_uri": image_uri,
        "voice_ref": voice_ref,
        "voice_preview_uri": voice_preview_uri,
        "image": image_uri,
        "voice": voice_ref,
        "angles": list(creator.get("angles") or []),
        "voice_reroll_count": creator.get("voice_reroll_count"),
    }
