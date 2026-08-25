"""Inferência canônica de gênero com fallback determinístico por paridade.

Fonte única dos tokens usados por ``adapters.base`` (perfil de voz) e
``tools.creators`` (spec de voz). O match considera palavras Unicode completas;
tokens femininos são testados antes dos masculinos, como nos sites originais.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

GenderPreset = Literal["female", "male", "neutral"]
ConcreteGender = Literal["female", "male"]

_FEMALE_TOKENS: tuple[str, ...] = (
    "female",
    "feminina",
    "feminino",
    "woman",
    "women",
    "mulher",
    "girl",
    "moça",
    "garota",
    "ela",
    "her",
)
_MALE_TOKENS: tuple[str, ...] = (
    "male",
    "masculina",
    "masculino",
    "man",
    "men",
    "homem",
    "boy",
    "rapaz",
    "moço",
    "garoto",
    "ele",
    "his",
)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def infer_gender(text: Optional[str]) -> GenderPreset:
    """Infere ``female``/``male`` a partir de tokens; vazio ou silencioso -> ``neutral``."""
    lowered = (text or "").casefold()
    if not lowered:
        return "neutral"
    words = frozenset(_WORD_RE.findall(lowered))
    if any(token in words for token in _FEMALE_TOKENS):
        return "female"
    if any(token in words for token in _MALE_TOKENS):
        return "male"
    return "neutral"


def gender_by_parity(index: int) -> ConcreteGender:
    """Gênero concreto determinístico: índice par -> ``female``, ímpar -> ``male``."""
    return "female" if index % 2 == 0 else "male"
