"""Seleção do backend de storage por config (D30, Fase 3).

A ADR-D30 exige que o backend seja configurável e que ``config-mock`` continue local,
offline e sem custo. Nenhum teste aqui toca rede ou credencial.
"""
from __future__ import annotations

import pytest

from orchestrator.storage.factory import build_media_storage
from orchestrator.storage.local import LocalMediaStorage
from orchestrator.storage.r2 import R2MediaStorage


def test_defaults_to_local_when_providers_says_nothing(tmp_path):
    """Sem config de storage, o comportamento é o histórico: disco local."""
    storage = build_media_storage({}, root=tmp_path, web_prefix="/media")

    assert isinstance(storage, LocalMediaStorage)


def test_explicit_local_backend(tmp_path):
    storage = build_media_storage({"storage": {"backend": "local"}}, root=tmp_path, web_prefix="/media")

    assert isinstance(storage, LocalMediaStorage)


def test_local_backend_serves_from_the_given_root_and_prefix(tmp_path):
    storage = build_media_storage({}, root=tmp_path, web_prefix="/videos")

    assert storage._root == tmp_path
    assert storage._web_prefix == "/videos"


def test_r2_backend_is_built_from_env(tmp_path, monkeypatch):
    import orchestrator.storage.r2 as r2_module

    monkeypatch.setattr(r2_module.boto3, "client", lambda service, **kw: object())
    for var, val in [
        ("R2_ACCOUNT_ID", "acct"), ("R2_ACCESS_KEY_ID", "ak"),
        ("R2_SECRET_ACCESS_KEY", "sk"), ("R2_BUCKET", "ugc"),
    ]:
        monkeypatch.setenv(var, val)

    storage = build_media_storage({"storage": {"backend": "r2"}}, root=tmp_path, web_prefix="/media")

    assert isinstance(storage, R2MediaStorage)
    assert storage.bucket == "ugc"


def test_an_unknown_backend_fails_loudly(tmp_path):
    """Typo em providers.yaml não pode degradar silenciosamente para disco local."""
    with pytest.raises(ValueError, match="unknown storage backend 'gcs'"):
        build_media_storage({"storage": {"backend": "gcs"}}, root=tmp_path, web_prefix="/media")


def test_none_providers_is_treated_as_empty(tmp_path):
    assert isinstance(build_media_storage(None, root=tmp_path, web_prefix="/media"), LocalMediaStorage)
