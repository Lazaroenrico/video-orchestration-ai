"""Migração idempotente do estado local anterior à ADR-D36."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3

import pytest
from click.testing import CliRunner
from langgraph.checkpoint.base import empty_checkpoint

from orchestrator.cli import cli
from orchestrator.db import (
    Database,
    PostgresCreatorRepository,
    PostgresPromptRepository,
    TenantIdentity,
    provision_runtime_role,
    upgrade_database,
)
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.legacy_import import (
    LegacyImportDriftError,
    apply_legacy,
    scan_legacy,
)
from orchestrator.storage.base import StoredObject, ext_from_mime


def _legacy_fixture(root, *, with_runs=True):
    if with_runs:
        runs = sqlite3.connect(root / "runs.sqlite")
        runs.executescript(
            """
        CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT,
            checkpoint BLOB, metadata BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        );
        CREATE TABLE writes (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL,
            channel TEXT NOT NULL, type TEXT, value BLOB,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        );
        INSERT INTO checkpoints VALUES ('run-1', '', 'cp-1', NULL, 'msgpack', X'01', X'02');
        INSERT INTO writes VALUES ('run-1', '', 'cp-1', 'task-1', 0, 'results', 'msgpack', X'03');
        """
        )
        runs.close()

    payload = b"legacy-image"
    media = root / "media" / "run-1" / "creator-0"
    media.mkdir(parents=True)
    (media / "image.png").write_bytes(payload)
    artifacts = sqlite3.connect(root / "artifacts.sqlite")
    artifacts.executescript(
        """
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, item_id TEXT, creator_id TEXT,
            kind TEXT NOT NULL, storage_backend TEXT NOT NULL, storage_key TEXT NOT NULL,
            content_type TEXT, size_bytes INTEGER, sha256 TEXT, source_uri TEXT,
            retention_class TEXT NOT NULL, expires_at TEXT, meta_json TEXT NOT NULL
        );
        """
    )
    artifacts.execute(
        "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "artifact-1", "run-1", None, "creator-0", "image", "local",
            "run-1/creator-0/image.png", "image/png", len(payload), None, None,
            "keep", None, "{}",
        ),
    )
    artifacts.commit()
    artifacts.close()

    (root / "creators.json").write_text(
        json.dumps(
            {
                "run-1:creator-0": {
                    "run_id": "run-1",
                    "creator_id": "creator-0",
                    "image": "/media/run-1/creator-0/image.png",
                    "voice": "voice-provider-id",
                    "voice_preview": "data:audio/wav;base64,bGVnYWN5LXZvaWNl",
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "prompts.json").write_text(
        json.dumps(
            {
                "templates": {
                    "1": {
                        "_idx": 0,
                        "kind": "creator",
                        "title": "Legacy portrait",
                        "text": "portrait",
                        "desc": "Imported",
                    }
                },
                "last_used": {"creator": "portrait", "video": "demo"},
            }
        ),
        encoding="utf-8",
    )
    (root / "feedback.json").write_text(
        json.dumps({"run-1": {"_idx": 0, "approved": 1}}),
        encoding="utf-8",
    )


async def _valid_legacy_checkpoint(root):
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "00000000-0000-0000-0000-000000000001"
    checkpoint["channel_values"] = {
        "run_id": "run-1",
        "config": {"offer": "legacy serum", "batch_size": 1},
        "results": [],
    }
    checkpoint["channel_versions"] = {
        key: "00000000000000000000000000000001.0.0"
        for key in checkpoint["channel_values"]
    }
    async with open_checkpointer(root / "runs.sqlite") as saver:
        saved = await saver.aput(
            {"configurable": {"thread_id": "run-1", "checkpoint_ns": ""}},
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            checkpoint["channel_versions"],
        )
        await saver.aput_writes(saved, [("result", {"ok": True})], "task-1")


class _MemoryStorage:
    backend = "r2"

    def __init__(self):
        self.objects = {}

    async def put_bytes(self, data, *, key_base, content_type):
        key = f"{key_base}.{ext_from_mime(content_type)}"
        stored = StoredObject(
            backend=self.backend,
            key=key,
            uri=f"r2://test/{key}",
            content_type=content_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        self.objects[key] = bytes(data)
        return stored

    async def exists(self, key):
        return key in self.objects


class _FailOnceStorage(_MemoryStorage):
    def __init__(self):
        super().__init__()
        self._should_fail = True

    async def put_bytes(self, data, *, key_base, content_type):
        if self._should_fail:
            self._should_fail = False
            raise RuntimeError("storage temporarily unavailable")
        return await super().put_bytes(
            data,
            key_base=key_base,
            content_type=content_type,
        )


class _ConcurrentStorage(_MemoryStorage):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.first_upload_started = asyncio.Event()
        self.concurrent_upload_started = asyncio.Event()

    async def put_bytes(self, data, *, key_base, content_type):
        self.calls += 1
        if self.calls == 1:
            self.first_upload_started.set()
            try:
                await asyncio.wait_for(
                    self.concurrent_upload_started.wait(),
                    timeout=2,
                )
            except TimeoutError:
                pass
        else:
            self.concurrent_upload_started.set()
        return await super().put_bytes(
            data,
            key_base=key_base,
            content_type=content_type,
        )


class _DivergentStorage(_MemoryStorage):
    def __init__(self, target):
        super().__init__()
        self.target = target

    async def put_bytes(self, data, *, key_base, content_type):
        stored = await super().put_bytes(
            data,
            key_base=key_base,
            content_type=content_type,
        )
        is_creator = "/legacy/" in key_base and "/creators/" in key_base
        should_corrupt = (
            self.target == "creator" and is_creator
        ) or (
            self.target == "artifact" and not is_creator
        )
        if not should_corrupt:
            return stored
        return StoredObject(
            backend=stored.backend,
            key=stored.key,
            uri=stored.uri,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256="0" * 64,
        )


def _database_url(postgresql, user=None, password=None):
    info = postgresql.info
    credentials = user or info.user
    if password is not None:
        credentials = f"{credentials}:{password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


def test_scan_legacy_builds_a_deterministic_complete_manifest(tmp_path):
    _legacy_fixture(tmp_path)

    first = scan_legacy(tmp_path)
    second = scan_legacy(tmp_path)

    assert first == second
    assert first.counts == {
        "runs": 1,
        "checkpoints": 1,
        "writes": 1,
        "artifacts": 1,
        "artifact_bytes": len(b"legacy-image"),
        "creators": 1,
        "creator_assets": 2,
        "prompt_last_used": 2,
        "feedback": 1,
    }
    assert len(first.checksum) == 64


def test_scan_legacy_requires_both_sqlite_sources(tmp_path):
    with pytest.raises(ValueError, match="runs.sqlite"):
        scan_legacy(tmp_path)


def test_scan_legacy_accepts_missing_optional_json_stores(tmp_path):
    _legacy_fixture(tmp_path)
    for name in ("creators.json", "prompts.json", "feedback.json"):
        (tmp_path / name).unlink()

    manifest = scan_legacy(tmp_path)

    assert manifest.counts["creators"] == 0
    assert manifest.counts["prompt_last_used"] == 0
    assert manifest.counts["feedback"] == 0


def test_scan_legacy_rejects_a_non_object_json_store(tmp_path):
    _legacy_fixture(tmp_path)
    (tmp_path / "creators.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="objeto JSON"):
        scan_legacy(tmp_path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "artifact local ausente"),
        ("size", "tamanho divergente"),
        ("sha256", "SHA-256 divergente"),
    ],
)
def test_scan_legacy_rejects_inconsistent_artifact_bytes(
    tmp_path,
    mutation,
    message,
):
    _legacy_fixture(tmp_path)
    if mutation == "missing":
        (tmp_path / "media" / "run-1" / "creator-0" / "image.png").unlink()
    else:
        with sqlite3.connect(tmp_path / "artifacts.sqlite") as connection:
            if mutation == "size":
                connection.execute(
                    "UPDATE artifacts SET size_bytes = 999 WHERE id = 'artifact-1'"
                )
            else:
                connection.execute(
                    "UPDATE artifacts SET sha256 = ? WHERE id = 'artifact-1'",
                    ("0" * 64,),
                )

    with pytest.raises(ValueError, match=message):
        scan_legacy(tmp_path)


def test_import_legacy_cli_is_dry_run_by_default(tmp_path):
    _legacy_fixture(tmp_path)

    result = CliRunner().invoke(cli, ["import-legacy", "--legacy-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["mode"] == "dry-run"
    assert report["counts"]["checkpoints"] == 1
    assert report["counts"]["artifacts"] == 1
    assert len(report["checksum"]) == 64


def test_import_legacy_cli_requires_database_url_for_apply(
    tmp_path,
    monkeypatch,
):
    _legacy_fixture(tmp_path)
    monkeypatch.setattr("orchestrator.cli.load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = CliRunner().invoke(
        cli,
        ["import-legacy", "--legacy-root", str(tmp_path), "--apply"],
    )

    assert result.exit_code != 0
    assert "DATABASE_URL" in result.output


def test_import_legacy_cli_reports_invalid_storage_configuration(
    tmp_path,
    monkeypatch,
):
    _legacy_fixture(tmp_path)
    monkeypatch.setattr(
        "orchestrator.cli.build_media_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("invalid storage profile")
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["import-legacy", "--legacy-root", str(tmp_path), "--apply"],
        env={"DATABASE_URL": "postgresql://unused"},
    )

    assert result.exit_code != 0
    assert "invalid storage profile" in result.output


def test_import_legacy_cli_applies_and_reports_noop_on_reexecution(
    tmp_path,
    postgresql,
    monkeypatch,
):
    _legacy_fixture(tmp_path, with_runs=False)
    asyncio.run(_valid_legacy_checkpoint(tmp_path))
    admin_url = _database_url(postgresql)
    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    runtime_url = _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )
    storage = _MemoryStorage()
    monkeypatch.setattr(
        "orchestrator.cli.build_media_storage",
        lambda *_args, **_kwargs: storage,
    )
    env = {
        "DATABASE_URL": runtime_url,
        "ORCH_ORGANIZATION_SLUG": "cli-acme",
        "ORCH_ORGANIZATION_NAME": "CLI Acme",
        "ORCH_USER_SUBJECT": "migration|cli-acme",
    }
    command = [
        "import-legacy",
        "--legacy-root",
        str(tmp_path),
        "--source-id",
        "cli-fixture",
        "--config-dir",
        "config-mock",
        "--apply",
    ]

    first = CliRunner().invoke(cli, command, env=env)
    second = CliRunner().invoke(cli, command, env=env)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert json.loads(first.output)["mode"] == "applied"
    assert json.loads(second.output)["mode"] == "noop"
    assert len(storage.objects) == 3


async def test_apply_legacy_is_complete_and_reexecution_is_noop(
    tmp_path, postgresql, monkeypatch
):
    _legacy_fixture(tmp_path, with_runs=False)
    await _valid_legacy_checkpoint(tmp_path)
    manifest = scan_legacy(tmp_path)
    admin_url = _database_url(postgresql)
    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    runtime_url = _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )
    storage = _MemoryStorage()

    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "imported-acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Imported Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "migration|imported-acme")
    async with Database(runtime_url) as database:
        first = await apply_legacy(
            manifest,
            database=database,
            database_url=runtime_url,
            storage=storage,
            source_id="fixture",
        )
        second = await apply_legacy(
            manifest,
            database=database,
            database_url=runtime_url,
            storage=storage,
            source_id="fixture",
        )
        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        imported_creator = await PostgresCreatorRepository(
            database,
            tenant,
        ).find_creator("creator-0", "run-1")
        imported_prompts = await PostgresPromptRepository(
            database,
            tenant,
        ).list_templates()
        (tmp_path / "prompts.json").write_text(
            json.dumps({"last_used": {"creator": "changed", "video": "demo"}}),
            encoding="utf-8",
        )
        drifted_manifest = scan_legacy(tmp_path)
        with pytest.raises(LegacyImportDriftError, match="mudou"):
            await apply_legacy(
                drifted_manifest,
                database=database,
                database_url=runtime_url,
                storage=storage,
                source_id="fixture",
            )

    assert first.mode == "applied"
    assert second.mode == "noop"
    assert first.counts == manifest.counts
    assert len(storage.objects) == 3
    assert all(key.startswith("tenants/") for key in storage.objects)
    assert imported_creator is not None
    assert imported_creator["image_uri"].startswith("r2://test/tenants/")
    assert imported_creator["voice_preview_uri"].startswith("r2://test/tenants/")
    assert imported_creator["voice_ref"] == "voice-provider-id"
    assert imported_prompts[0]["title"] == "Legacy portrait"
    async with open_checkpointer(tmp_path / "must-not-exist.sqlite") as saver:
        imported = await saver.aget_tuple(
            {"configurable": {"thread_id": "run-1", "checkpoint_ns": ""}}
        )
    assert imported is not None
    assert imported.checkpoint["channel_values"]["run_id"] == "run-1"
    assert not (tmp_path / "must-not-exist.sqlite").exists()


async def test_failed_import_is_recorded_and_can_be_retried(
    tmp_path,
    postgresql,
    monkeypatch,
):
    _legacy_fixture(tmp_path, with_runs=False)
    await _valid_legacy_checkpoint(tmp_path)
    manifest = scan_legacy(tmp_path)
    admin_url = _database_url(postgresql)
    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    runtime_url = _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "retry-acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Retry Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "migration|retry-acme")
    storage = _FailOnceStorage()

    async with Database(runtime_url) as database:
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            await apply_legacy(
                manifest,
                database=database,
                database_url=runtime_url,
                storage=storage,
                source_id="retry-fixture",
            )

        tenant = await database.ensure_tenant(TenantIdentity.from_env())
        async with database.connection(tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT status, error
                FROM legacy_import_batches
                WHERE organization_id = %s AND source_id = 'retry-fixture'
                """,
                (tenant.organization_id,),
            )
            failed_batch = await cursor.fetchone()

        retried = await apply_legacy(
            manifest,
            database=database,
            database_url=runtime_url,
            storage=storage,
            source_id="retry-fixture",
        )

    assert failed_batch == ("failed", "storage temporarily unavailable")
    assert retried.mode == "applied"


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("creator", "storage divergiu para asset de creator"),
        ("artifact", "storage divergiu para artifact"),
    ],
)
async def test_import_rejects_storage_integrity_mismatch(
    tmp_path,
    postgresql,
    monkeypatch,
    target,
    message,
):
    _legacy_fixture(tmp_path, with_runs=False)
    await _valid_legacy_checkpoint(tmp_path)
    manifest = scan_legacy(tmp_path)
    admin_url = _database_url(postgresql)
    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    runtime_url = _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", f"{target}-integrity")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", f"{target.title()} Integrity")
    monkeypatch.setenv("ORCH_USER_SUBJECT", f"migration|{target}-integrity")

    async with Database(runtime_url) as database:
        with pytest.raises(ValueError, match=message):
            await apply_legacy(
                manifest,
                database=database,
                database_url=runtime_url,
                storage=_DivergentStorage(target),
                source_id=f"{target}-integrity",
            )


async def test_concurrent_imports_for_the_same_source_are_serialized(
    tmp_path,
    postgresql,
    monkeypatch,
):
    _legacy_fixture(tmp_path, with_runs=False)
    await _valid_legacy_checkpoint(tmp_path)
    manifest = scan_legacy(tmp_path)
    admin_url = _database_url(postgresql)
    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    runtime_url = _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "concurrent-acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Concurrent Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "migration|concurrent-acme")
    storage = _ConcurrentStorage()

    async with Database(runtime_url) as database:
        first = asyncio.create_task(
            apply_legacy(
                manifest,
                database=database,
                database_url=runtime_url,
                storage=storage,
                source_id="concurrent-fixture",
            )
        )
        await storage.first_upload_started.wait()
        second = asyncio.create_task(
            apply_legacy(
                manifest,
                database=database,
                database_url=runtime_url,
                storage=storage,
                source_id="concurrent-fixture",
            )
        )
        results = await asyncio.gather(first, second)

    assert sorted(result.mode for result in results) == ["applied", "noop"]
    assert storage.calls == 3
