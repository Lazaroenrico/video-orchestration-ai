"""Resolução de ponteiros r2:// em signed URLs sob demanda (D30, Fase 4).

D30: "URLs assinadas devem ser geradas apenas quando algum consumidor precisa acessar os
bytes" e "não devem substituir storage_key no DB". Aqui o ponteiro canônico vira URL
assinada **na saída**, sem nunca ser persistido.
"""
from __future__ import annotations

import pytest

from orchestrator.storage.resolve import r2_key_from_uri, resolve_signed_uris


class _FakeStorage:
    """Assina de forma previsível e conta as chamadas."""

    backend = "r2"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def get_signed_url(self, key: str, *, ttl_seconds: int = 900) -> str:
        self.calls.append((key, ttl_seconds))
        return f"https://signed.example/{key}?ttl={ttl_seconds}"


@pytest.fixture
def storage() -> _FakeStorage:
    return _FakeStorage()


# --------------------------------------------------------------------------- #
# r2_key_from_uri                                                              #
# --------------------------------------------------------------------------- #


def test_extracts_the_key_from_a_canonical_pointer():
    assert r2_key_from_uri("r2://ugc/run-1/items/item-0/clip-0.mp4") == "run-1/items/item-0/clip-0.mp4"


@pytest.mark.parametrize(
    "uri",
    [
        "/videos/run-1/clip-0.mp4",  # local já servível
        "https://cdn.example/a.mp4",
        "data:video/mp4;base64,AA==",
        "mock://video/abc",
        "voice-0",
        "",
        "r2://ugc",  # sem key
        "r2://ugc/",  # key vazia
    ],
)
def test_returns_none_for_anything_that_is_not_an_r2_pointer(uri):
    assert r2_key_from_uri(uri) is None


# --------------------------------------------------------------------------- #
# resolve_signed_uris                                                          #
# --------------------------------------------------------------------------- #


async def test_rewrites_an_r2_pointer_into_a_signed_url(storage):
    payload = {"uri": "r2://ugc/run-1/items/item-0/clip-0.mp4"}

    out = await resolve_signed_uris(payload, storage=storage)

    assert out["uri"] == "https://signed.example/run-1/items/item-0/clip-0.mp4?ttl=900"


async def test_walks_nested_dicts_and_lists(storage):
    payload = {
        "results": [
            {"clips": [{"uri": "r2://ugc/a.mp4"}, {"uri": "r2://ugc/b.mp4"}]},
            {"assembled": {"uri": "r2://ugc/c.mp4"}},
        ],
        "creators": [{"upscaled_base": "r2://ugc/d.png"}],
    }

    out = await resolve_signed_uris(payload, storage=storage)

    assert out["results"][0]["clips"][0]["uri"].startswith("https://signed.example/a.mp4")
    assert out["results"][0]["clips"][1]["uri"].startswith("https://signed.example/b.mp4")
    assert out["results"][1]["assembled"]["uri"].startswith("https://signed.example/c.mp4")
    assert out["creators"][0]["upscaled_base"].startswith("https://signed.example/d.png")


async def test_leaves_every_other_uri_untouched(storage):
    payload = {
        "local": "/videos/run-1/clip-0.mp4",
        "http": "https://cdn.example/a.mp4",
        "data": "data:video/mp4;base64,AA==",
        "mock": "mock://video/abc",
        "number": 42,
        "none": None,
    }

    out = await resolve_signed_uris(payload, storage=storage)

    assert out == payload
    assert storage.calls == []


async def test_the_canonical_pointer_is_never_mutated_in_place(storage):
    """O payload de saída é uma cópia: a verdade a montante segue sendo o ponteiro."""
    payload = {"uri": "r2://ugc/a.mp4"}

    await resolve_signed_uris(payload, storage=storage)

    assert payload["uri"] == "r2://ugc/a.mp4"


async def test_the_same_key_is_signed_only_once(storage):
    """Um clip aparece em results e em artifacts: assinar 2x é HMAC jogado fora."""
    payload = {"a": {"uri": "r2://ugc/same.mp4"}, "b": {"uri": "r2://ugc/same.mp4"}}

    out = await resolve_signed_uris(payload, storage=storage)

    assert storage.calls == [("same.mp4", 900)]
    assert out["a"]["uri"] == out["b"]["uri"]


async def test_the_ttl_is_short_and_configurable(storage):
    await resolve_signed_uris({"uri": "r2://ugc/a.mp4"}, storage=storage, ttl_seconds=120)

    assert storage.calls == [("a.mp4", 120)]


async def test_without_a_signing_storage_the_pointer_is_left_alone(storage):
    """Backend local não assina — e o payload não pode quebrar por isso."""
    payload = {"uri": "r2://ugc/a.mp4"}

    assert await resolve_signed_uris(payload, storage=None) == payload
