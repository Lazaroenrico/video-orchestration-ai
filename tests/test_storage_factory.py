"""Seleção do backend de storage por config (D30, Fase 3).

A ADR-D30 exige que o backend seja configurável e que ``config-mock`` continue local,
offline e sem custo. Nenhum teste aqui toca rede ou credencial.
"""
from __future__ import annotations

import pytest

from orchestrator.storage.factory import build_media_storage
from orchestrator.storage.local import LocalMediaStorage
from orchestrator.storage.multi import MultiBackendMediaStorage
from orchestrator.storage.r2 import R2MediaStorage
from orchestrator.storage.s3 import S3MediaStorage


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


async def test_s3_backend_uses_the_aws_role_and_returns_s3_pointers(
    tmp_path,
    monkeypatch,
):
    import orchestrator.storage.s3 as s3_module

    captured: dict = {}

    class _Client:
        def put_object(self, **kwargs):
            captured["put"] = kwargs

    def fake_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return _Client()

    monkeypatch.setattr(s3_module.boto3, "client", fake_client)
    monkeypatch.setenv("S3_BUCKET", "ugc-aws")
    monkeypatch.setenv("AWS_REGION", "sa-east-1")

    storage = build_media_storage(
        {"storage": {"backend": "s3"}},
        root=tmp_path,
        web_prefix="/media",
    )

    assert isinstance(storage, S3MediaStorage)
    assert storage.backend == "s3"
    assert storage.bucket == "ugc-aws"
    assert captured["service"] == "s3"
    assert captured["region_name"] == "sa-east-1"
    stored = await storage.put_bytes(
        b"video",
        key_base="run-1/final",
        content_type="video/mp4",
    )
    assert stored.uri == "s3://ugc-aws/run-1/final.mp4"


async def test_dual_backend_writes_s3_and_reads_each_original_backend(
    tmp_path,
    monkeypatch,
):
    class _Storage:
        def __init__(self, backend: str) -> None:
            self.backend = backend
            self.calls: list[str] = []

        async def put_bytes(self, data, *, key_base, content_type):
            self.calls.append(f"put:{key_base}")
            return {"backend": self.backend, "data": data}

        async def put_from_url(self, uri, *, key_base, client=None):
            self.calls.append(f"from:{uri}:{key_base}:{client}")
            return {"backend": self.backend, "uri": uri}

        async def get_signed_url(self, key, *, ttl_seconds=900):
            self.calls.append(f"sign:{key}:{ttl_seconds}")
            return f"https://{self.backend}.example/{key}"

        async def delete(self, key):
            self.calls.append(f"delete:{key}")

        async def exists(self, key):
            self.calls.append(f"exists:{key}")
            return key != "missing"

    r2 = _Storage("r2")
    s3 = _Storage("s3")
    monkeypatch.setattr(R2MediaStorage, "from_env", lambda: r2)
    monkeypatch.setattr(S3MediaStorage, "from_env", lambda: s3)

    storage = build_media_storage(
        {"storage": {"backend": "dual", "write_backend": "s3"}},
        root=tmp_path,
        web_prefix="/media",
    )
    written = await storage.put_bytes(
        b"video",
        key_base="run-1/final",
        content_type="video/mp4",
    )

    assert isinstance(storage, MultiBackendMediaStorage)
    assert storage.backend == "s3"
    assert written == {"backend": "s3", "data": b"video"}
    assert await storage.get_signed_url_for("r2", "old.mp4") == (
        "https://r2.example/old.mp4"
    )
    assert await storage.get_signed_url_for("s3", "new.mp4") == (
        "https://s3.example/new.mp4"
    )
    assert await storage.put_from_url(
        "https://source/video.mp4",
        key_base="run-1/source",
        client="client",
    ) == {
        "backend": "s3",
        "uri": "https://source/video.mp4",
    }
    assert await storage.get_signed_url("current.mp4") == (
        "https://s3.example/current.mp4"
    )
    await storage.delete("current.mp4")
    await storage.delete_from("r2", "old.mp4")
    assert await storage.exists("current.mp4") is True
    assert await storage.exists_in("r2", "missing") is False
    with pytest.raises(ValueError, match="desconhecido"):
        await storage.get_signed_url_for("gcs", "bad.mp4")
    with pytest.raises(ValueError, match="write backend"):
        MultiBackendMediaStorage({"r2": r2}, write_backend="s3")


def test_runtime_env_can_switch_the_same_image_to_aws_storage(tmp_path, monkeypatch):
    marker = object()
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setattr(S3MediaStorage, "from_env", lambda: marker)

    storage = build_media_storage(
        {"storage": {"backend": "r2"}},
        root=tmp_path,
        web_prefix="/media",
    )

    assert storage is marker


def test_dev_storage_override_takes_precedence_over_runtime_and_profile(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ORCH_DEV_STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_BACKEND", "s3")

    storage = build_media_storage(
        {"storage": {"backend": "r2"}},
        root=tmp_path,
        web_prefix="/media",
    )

    assert isinstance(storage, LocalMediaStorage)


def test_s3_backend_fails_fast_without_role_region_or_bucket(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    with pytest.raises(ValueError, match="S3_BUCKET, AWS_REGION"):
        build_media_storage(
            {"storage": {"backend": "s3"}},
            root=tmp_path,
            web_prefix="/media",
        )
