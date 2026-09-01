"""Mídia sintética determinística compartilhada por mocks e previews."""

from __future__ import annotations

import base64
import hashlib
from typing import Any


def wav_data_uri(*seed_parts: Any) -> str:
    """WAV PCM 8-bit mono minúsculo e determinístico para preview offline.

    As amostras derivam exclusivamente de ``sha256("|".join(seed_parts))`` —
    sem ``random``. Callers que precisam de um namespace próprio (ex.: o
    ``MockAdapter`` usa o prefixo ``"voice-preview"``) incluem esse prefixo
    como primeiro seed part.
    """
    sample_rate = 4000
    n_samples = 400
    digest = hashlib.sha256("|".join(str(p) for p in seed_parts).encode()).digest()
    samples = bytes(digest[i % len(digest)] for i in range(n_samples))
    data_size = len(samples)
    byte_rate = sample_rate
    header = (
        b"RIFF"
        + (36 + data_size).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (8).to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )
    return "data:audio/wav;base64," + base64.b64encode(header + samples).decode()
