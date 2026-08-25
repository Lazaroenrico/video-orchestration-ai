"""Primitivas consolidadas em ``orchestrator.common`` — fonte única de verdade."""

from __future__ import annotations

import base64
import hashlib
import struct
from datetime import datetime
from typing import Any

import pytest
from pydantic import BaseModel

from orchestrator.common.gender import gender_by_parity, infer_gender
from orchestrator.common.media import wav_data_uri
from orchestrator.common.plain import to_plain
from orchestrator.common.statuses import TERMINAL_PREDICTION_STATUSES


class _Inner(BaseModel):
    at: datetime
    tags: list[str]


class _Outer(BaseModel):
    inner: _Inner
    mapping: dict[int, str]
    pair: tuple[str, int]


def _wav_bytes(uri: str) -> bytes:
    assert uri.startswith("data:audio/wav;base64,")
    return base64.b64decode(uri.removeprefix("data:audio/wav;base64,"))


def test_to_plain_converts_pydantic_models_recursively_in_json_mode() -> None:
    value = _Outer(
        inner=_Inner(at=datetime(2026, 1, 2, 3, 4, 5), tags=["a", "b"]),
        mapping={1: "x"},
        pair=("k", 7),
    )
    assert to_plain(value) == {
        "inner": {"at": "2026-01-02T03:04:05", "tags": ["a", "b"]},
        "mapping": {"1": "x"},
        "pair": ["k", 7],
    }


def test_to_plain_walks_nested_containers_and_stringifies_keys() -> None:
    value = {1: [{"a": (True, None)}, "tail"]}
    assert to_plain(value) == {"1": [{"a": [True, None]}, "tail"]}


def test_to_plain_keeps_scalar_datetime_outside_models_untouched() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5)
    result = to_plain({"at": moment})
    assert result["at"] is moment


def test_to_plain_passthrough_scalars() -> None:
    for scalar in ("text", 3, 1.5, True, None):
        assert to_plain(scalar) == scalar


def test_wav_data_uri_is_deterministic_and_valid_wav() -> None:
    first = wav_data_uri("run-1", "creator-0", 1)
    again = wav_data_uri("run-1", "creator-0", 1)
    other = wav_data_uri("run-1", "creator-0", 2)
    assert first == again
    assert first != other
    raw = _wav_bytes(first)
    assert raw[:4] == b"RIFF"
    assert raw[8:12] == b"WAVE"
    assert struct.unpack("<I", raw[4:8])[0] == len(raw) - 8


def test_wav_data_uri_preserves_legacy_stage_seeding() -> None:
    parts: tuple[Any, ...] = ("run-1", "creator-3", 2, "")
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    samples = bytes(digest[i % len(digest)] for i in range(400))
    expected_prefix = b"RIFF" + (36 + len(samples)).to_bytes(4, "little") + b"WAVEfmt "
    raw = _wav_bytes(wav_data_uri(*parts))
    assert raw[:16] == expected_prefix
    assert raw[44:] == samples


def test_terminal_prediction_statuses_is_the_canonical_frozenset() -> None:
    assert isinstance(TERMINAL_PREDICTION_STATUSES, frozenset)
    assert TERMINAL_PREDICTION_STATUSES == frozenset({"succeeded", "failed", "canceled"})
    assert "processing" not in TERMINAL_PREDICTION_STATUSES
    assert "starting" not in TERMINAL_PREDICTION_STATUSES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("energetic female skincare creator", "female"),
        ("energetic male skincare creator", "male"),
        ("criadora mulher adulta", "female"),
        ("perfil feminina", "female"),
        ("estilo feminino", "female"),
        ("an energetic woman", "female"),
        ("reviews from women", "female"),
        ("girl next door", "female"),
        ("moça do produto", "female"),
        ("garota ensina", "female"),
        ("ela apresenta o produto", "female"),
        ("shares her routine", "female"),
        ("um homem prático", "male"),
        ("tom masculino", "male"),
        ("voz masculina", "male"),
        ("the man explains", "male"),
        ("made for men", "male"),
        ("boy next door", "male"),
        ("rapaz animado", "male"),
        ("moço do demo", "male"),
        ("garoto mostra", "male"),
        ("ele demonstra", "male"),
        ("his morning routine", "male"),
        ("friendly creator", "neutral"),
        ("", "neutral"),
        (None, "neutral"),
    ],
)
def test_infer_gender_covers_merged_token_superset(text: str | None, expected: str) -> None:
    assert infer_gender(text) == expected


@pytest.mark.parametrize("text", ["other", "sherpa", "elegante", "elemento"])
def test_infer_gender_does_not_match_tokens_inside_other_words(text: str) -> None:
    assert infer_gender(text) == "neutral"


@pytest.mark.parametrize(
    ("index", "expected"),
    [(0, "female"), (1, "male"), (2, "female"), (3, "male"), (10, "female")],
)
def test_gender_by_parity_matches_legacy_rule(index: int, expected: str) -> None:
    assert gender_by_parity(index) == expected
