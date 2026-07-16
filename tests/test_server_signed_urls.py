"""Signed URLs sob demanda no contrato público da web (D30, Fase 4).

Consequência da ADR-D30: "a UI deixa de depender de paths permanentes em live e passa a
receber signed URLs sob demanda". Offline: o backend que assina é um stub.
"""
from __future__ import annotations

import pytest

from orchestrator.web import server


class _SigningStorage:
    backend = "r2"

    async def get_signed_url(self, key: str, *, ttl_seconds: int = 900) -> str:
        return f"https://signed.example/{key}?ttl={ttl_seconds}"


# --------------------------------------------------------------------------- #
# Renderabilidade do ponteiro                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("r2://ugc/run-1/items/i0/clip-0.mp4", True),
        ("r2://ugc/run-1/c0/image.png", True),
        ("r2://ugc/run-1/c0/voice.mp3", True),
        ("r2://ugc/run-1/c0/blob.bin", False),  # sem tipo de mídia conhecido
    ],
)
def test_an_r2_pointer_is_renderable_because_it_becomes_a_signed_url(uri, expected):
    """O ponteiro vira https assinado na saída, então a UI consegue tocar o objeto."""
    assert server._is_renderable_uri(uri) is expected


def test_an_r2_pointer_keeps_its_media_type_from_the_key_extension():
    assert server._media_type_for_uri("r2://ugc/run-1/items/i0/clip-0.mp4") == "video"


def test_an_unknown_scheme_is_still_not_renderable():
    """Só r2:// ganhou passe — s3://, gs:// etc. seguem sendo referência opaca."""
    assert server._is_renderable_uri("s3://bucket/a.mp4") is False


# --------------------------------------------------------------------------- #
# Seleção do backend que assina                                                #
# --------------------------------------------------------------------------- #


def test_the_local_backend_does_not_sign(monkeypatch):
    """Local serve /media direto do disco: assinar não faria sentido."""
    monkeypatch.setattr(server, "load_providers", lambda cd: {"storage": {"backend": "local"}})

    assert server._signing_storage(None) is None


def test_the_r2_backend_signs(monkeypatch):
    monkeypatch.setattr(server, "load_providers", lambda cd: {"storage": {"backend": "r2"}})
    monkeypatch.setattr(server, "build_media_storage", lambda *a, **k: _SigningStorage())

    assert isinstance(server._signing_storage(None), _SigningStorage)


def test_a_broken_storage_config_does_not_take_the_dashboard_down(monkeypatch):
    """Config inválida derruba o run (falha alto), mas não pode cegar a UI inteira."""
    monkeypatch.setattr(server, "load_providers", lambda cd: {"storage": {"backend": "gcs"}})

    assert server._signing_storage(None) is None


# --------------------------------------------------------------------------- #
# Resolução no payload                                                         #
# --------------------------------------------------------------------------- #


async def test_run_state_payload_has_its_pointers_signed(monkeypatch):
    monkeypatch.setattr(server, "_signing_storage", lambda cd: _SigningStorage())
    payload = {"items": [{"artifacts": [{"uri": "r2://ugc/run-1/items/i0/clip-0.mp4"}]}]}

    out = await server._sign_payload(payload, None)

    assert out["items"][0]["artifacts"][0]["uri"] == (
        "https://signed.example/run-1/items/i0/clip-0.mp4?ttl=900"
    )


async def test_a_local_payload_is_returned_untouched(monkeypatch):
    monkeypatch.setattr(server, "_signing_storage", lambda cd: None)
    payload = {"items": [{"artifacts": [{"uri": "/videos/run-1/items/i0/clip-0.mp4"}]}]}

    assert await server._sign_payload(payload, None) == payload
