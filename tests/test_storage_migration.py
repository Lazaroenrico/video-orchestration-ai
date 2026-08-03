"""Cópia R2 → S3 preservando identidade e integridade dos artifacts."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from click.testing import CliRunner

from orchestrator.cli import cli
from orchestrator.db import Database, TenantIdentity, upgrade_database
from orchestrator.db.artifacts import PostgresArtifactRepository
from orchestrator.storage.db import ArtifactRecord
from orchestrator.storage.migration import (
    BotoObjectStore,
    ObjectHead,
    ObjectIntegrityError,
    migrate_run_objects,
)


def _database_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_database_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE tenant_app LOGIN PASSWORD 'tenant_app';
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE tenant_app LOGIN PASSWORD 'tenant_app';
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO tenant_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tenant_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://tenant_app:tenant_app@{info.host}:{info.port}/{info.dbname}"


class _ObjectStore:
    def __init__(self, backend: str, objects: dict[str, bytes] | None = None) -> None:
        self.backend = backend
        self.objects = dict(objects or {})
        self.content_types: dict[str, str] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    async def get_object(self, key: str) -> bytes:
        return self.objects[key]

    async def put_object(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type
        self.metadata[key] = metadata

    async def head_object(self, key: str) -> ObjectHead:
        return ObjectHead(
            size_bytes=len(self.objects[key]),
            sha256=self.metadata[key]["sha256"],
        )


async def test_migrate_run_objects_preserves_key_checksum_and_metadata(postgresql):
    upgrade_database(_database_url(postgresql))
    data = b"portable-video"
    digest = hashlib.sha256(data).hexdigest()
    key = "acme/run-1/final.mp4"
    source = _ObjectStore("r2", {key: data})
    destination = _ObjectStore("s3")

    async with Database(_runtime_database_url(postgresql)) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        artifacts = PostgresArtifactRepository(database, tenant)
        original = ArtifactRecord(
            run_id="run-1",
            item_id="item-1",
            kind="assembled",
            storage_backend="r2",
            storage_key=key,
            content_type="video/mp4",
            size_bytes=len(data),
            sha256=digest,
            retention_class="keep",
            meta={"tier": "seedance"},
        )
        await artifacts.record(original)

        first = await migrate_run_objects(
            artifacts,
            run_id="run-1",
            source=source,
            destination=destination,
        )
        second = await migrate_run_objects(
            artifacts,
            run_id="run-1",
            source=source,
            destination=destination,
        )
        migrated = await artifacts.get(original.id)

    assert first == {
        "run_id": "run-1",
        "source_backend": "r2",
        "destination_backend": "s3",
        "copied": [key],
        "skipped": [],
    }
    assert second["copied"] == []
    assert second["skipped"] == [key]
    assert destination.objects[key] == data
    assert destination.content_types[key] == "video/mp4"
    assert destination.metadata[key] == {
        "sha256": digest,
        "run-id": "run-1",
        "artifact-id": original.id,
        "source-backend": "r2",
    }
    assert migrated is not None
    assert migrated.storage_backend == "s3"
    assert migrated.storage_key == original.storage_key
    assert migrated.sha256 == original.sha256
    assert migrated.meta == original.meta


async def test_migration_never_switches_backend_when_integrity_is_uncertain(postgresql):
    upgrade_database(_database_url(postgresql))
    key = "acme/run-bad/final.mp4"
    expected = hashlib.sha256(b"expected").hexdigest()

    async with Database(_runtime_database_url(postgresql)) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        artifacts = PostgresArtifactRepository(database, tenant)
        artifact = ArtifactRecord(
            run_id="run-bad",
            kind="assembled",
            storage_backend="r2",
            storage_key=key,
            content_type="video/mp4",
            size_bytes=8,
            sha256=expected,
        )
        await artifacts.record(artifact)

        with pytest.raises(ObjectIntegrityError, match="origem"):
            await migrate_run_objects(
                artifacts,
                run_id="run-bad",
                source=_ObjectStore("r2", {key: b"corrupt"}),
                destination=_ObjectStore("s3"),
            )
        assert (await artifacts.get(artifact.id)).storage_backend == "r2"

        class _CorruptHeadStore(_ObjectStore):
            async def head_object(self, key: str) -> ObjectHead:
                return ObjectHead(size_bytes=len(self.objects[key]), sha256="bad")

        with pytest.raises(ObjectIntegrityError, match="destino"):
            await migrate_run_objects(
                artifacts,
                run_id="run-bad",
                source=_ObjectStore("r2", {key: b"expected"}),
                destination=_CorruptHeadStore("s3"),
            )
        assert (await artifacts.get(artifact.id)).storage_backend == "r2"


async def test_boto_transfer_store_uses_exact_keys_and_checksum_metadata(monkeypatch):
    import orchestrator.storage.migration as migration_module

    calls: list[tuple[str, dict]] = []

    class _Body:
        def read(self):
            return b"bytes"

    class _Client:
        def get_object(self, **kwargs):
            calls.append(("get", kwargs))
            return {"Body": _Body()}

        def put_object(self, **kwargs):
            calls.append(("put", kwargs))

        def head_object(self, **kwargs):
            calls.append(("head", kwargs))
            return {
                "ContentLength": 5,
                "Metadata": {"sha256": hashlib.sha256(b"bytes").hexdigest()},
            }

    def fake_client(service: str, **kwargs):
        calls.append(("client", {"service": service, **kwargs}))
        return _Client()

    monkeypatch.setattr(migration_module.boto3, "client", fake_client)
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "r2-ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "r2-sk")
    monkeypatch.setenv("R2_BUCKET", "old")
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://r2.example")
    monkeypatch.setenv("S3_BUCKET", "new")
    monkeypatch.setenv("AWS_REGION", "sa-east-1")

    source = BotoObjectStore.from_r2_env()
    destination = BotoObjectStore.from_s3_env()
    data = await source.get_object("same/key.mp4")
    digest = hashlib.sha256(data).hexdigest()
    await destination.put_object(
        "same/key.mp4",
        data,
        content_type="video/mp4",
        metadata={"sha256": digest},
    )
    head = await destination.head_object("same/key.mp4")

    assert source.backend == "r2"
    assert destination.backend == "s3"
    assert head == ObjectHead(size_bytes=5, sha256=digest)
    assert ("get", {"Bucket": "old", "Key": "same/key.mp4"}) in calls
    put = next(payload for operation, payload in calls if operation == "put")
    assert put["Bucket"] == "new"
    assert put["Key"] == "same/key.mp4"
    assert put["Body"] == b"bytes"
    assert put["Metadata"] == {"sha256": digest}
    assert put["ChecksumSHA256"]


async def test_r2_transfer_store_uses_default_endpoint_and_no_native_checksum(
    monkeypatch,
):
    import orchestrator.storage.migration as migration_module

    captured = {}

    class _Client:
        def put_object(self, **kwargs):
            captured["put"] = kwargs

    def fake_client(_service, **kwargs):
        captured["client"] = kwargs
        return _Client()

    monkeypatch.setattr(migration_module.boto3, "client", fake_client)
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_BUCKET", "old")
    monkeypatch.delenv("R2_ENDPOINT_URL", raising=False)
    store = BotoObjectStore.from_r2_env()
    digest = hashlib.sha256(b"old").hexdigest()

    await store.put_object(
        "same/key.mp4",
        b"old",
        content_type="video/mp4",
        metadata={"sha256": digest},
    )

    assert captured["client"]["endpoint_url"] == (
        "https://acct.r2.cloudflarestorage.com"
    )
    assert "ChecksumSHA256" not in captured["put"]


def test_transfer_stores_fail_fast_when_environment_is_incomplete(monkeypatch):
    for name in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
        "S3_BUCKET",
        "AWS_REGION",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="R2_ACCOUNT_ID"):
        BotoObjectStore.from_r2_env()
    with pytest.raises(ValueError, match="S3_BUCKET"):
        BotoObjectStore.from_s3_env()


def test_storage_migrate_run_cli_executes_the_verified_copy(monkeypatch, postgresql):
    upgrade_database(_database_url(postgresql))
    runtime_url = _runtime_database_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    data = b"cli-video"
    digest = hashlib.sha256(data).hexdigest()
    key = "acme/run-cli/final.mp4"
    source = _ObjectStore("r2", {key: data})
    destination = _ObjectStore("s3")

    async def _prepare() -> None:
        async with Database(runtime_url) as database:
            tenant = await database.ensure_tenant(identity)
            await PostgresArtifactRepository(database, tenant).record(
                ArtifactRecord(
                    run_id="run-cli",
                    kind="assembled",
                    storage_backend="r2",
                    storage_key=key,
                    content_type="video/mp4",
                    size_bytes=len(data),
                    sha256=digest,
                )
            )

    asyncio.run(_prepare())
    monkeypatch.setattr(BotoObjectStore, "from_r2_env", lambda: source)
    monkeypatch.setattr(BotoObjectStore, "from_s3_env", lambda: destination)

    result = CliRunner().invoke(
        cli,
        ["storage", "migrate-run", "run-cli"],
        env={
            "DATABASE_URL": runtime_url,
            "ORCH_ORGANIZATION_SLUG": identity.organization_slug,
            "ORCH_ORGANIZATION_NAME": identity.organization_name,
            "ORCH_USER_SUBJECT": identity.user_subject,
        },
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["copied"] == [key]
    assert destination.objects[key] == data
