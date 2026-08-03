"""Vocabulário e validação compartilhados pelos backends de prompts."""
from __future__ import annotations

KINDS = ("creator", "video")


def validate_template(
    *,
    kind: str,
    title: str,
    text: str,
    desc: str,
) -> tuple[str, str, str, str]:
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r} (esperado um de {KINDS})")
    title = (title or "").strip()
    text = (text or "").strip()
    if not title:
        raise ValueError("title é obrigatório")
    if not text:
        raise ValueError("text é obrigatório")
    return kind, title, text, (desc or "").strip()
