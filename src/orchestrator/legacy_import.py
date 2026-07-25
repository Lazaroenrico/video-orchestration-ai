"""Inventário e importação do estado local anterior ao PostgreSQL."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from orchestrator import runner
from orchestrator.db import (
    Database,
    PostgresArtifactRepository,
    PostgresCreatorRepository,
    PostgresFeedbackRepository,
    PostgresPromptRepository,
    PostgresRunRepository,
    TenantIdentity,
)
from orchestrator.graph.checkpoint import (
    open_sqlite_checkpointer,
    open_tenant_postgres_checkpointer,
)
from orchestrator.storage.base import MediaStorage, decode_data_uri
from orchestrator.storage.db import ArtifactRecord


@dataclass(frozen=True)
class LegacyArtifact:
    row: tuple[Any, ...]
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class LegacyCreatorAsset:
    creator_key: str
    field: str
    source_ref: str
    data: bytes
    content_type: str
    sha256: str


@dataclass(frozen=True)
class LegacyManifest:
    root: Path
    checksum: str
    counts: dict[str, int]
    artifacts: tuple[LegacyArtifact, ...]
    creator_assets: tuple[LegacyCreatorAsset, ...]


@dataclass(frozen=True)
class LegacyImportResult:
    mode: str
    checksum: str
    counts: dict[str, int]


class LegacyImportDriftError(ValueError):
    """A origem já aplicada mudou e não pode sobrescrever o destino."""


def _json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"fonte legada deve conter objeto JSON: {path.name}")
    return data


def _artifact_path(root: Path, storage_key: str, content_type: str | None) -> Path:
    families = ("videos", "media") if (content_type or "").startswith("video/") else ("media", "videos")
    for family in families:
        candidate = root / family / storage_key
        if candidate.is_file():
            return candidate
    raise ValueError(f"artifact local ausente: {storage_key}")


def _digest_file(digest: Any, label: str, path: Path) -> None:
    digest.update(label.encode("utf-8"))
    digest.update(b"\0")
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)


def _creator_assets(
    root: Path,
    creators: dict[str, Any],
) -> tuple[LegacyCreatorAsset, ...]:
    assets: list[LegacyCreatorAsset] = []
    fields = (
        ("image", ("image_uri", "image")),
        ("voice_preview", ("voice_preview_uri", "voice_preview")),
    )
    for creator_key, creator in sorted(creators.items()):
        if not isinstance(creator, dict):
            continue
        for target_field, aliases in fields:
            source_ref = next(
                (
                    str(creator[name])
                    for name in aliases
                    if isinstance(creator.get(name), str) and creator[name]
                ),
                "",
            )
            if not source_ref:
                continue
            if source_ref.startswith("data:"):
                data, content_type = decode_data_uri(source_ref)
            elif source_ref.startswith(("/media/", "/videos/")):
                relative = source_ref.lstrip("/")
                path = root / relative
                if not path.is_file():
                    raise ValueError(f"asset de creator ausente: {source_ref}")
                data = path.read_bytes()
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            else:
                continue
            assets.append(
                LegacyCreatorAsset(
                    creator_key=str(creator_key),
                    field=target_field,
                    source_ref=source_ref,
                    data=data,
                    content_type=content_type,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
    return tuple(assets)


def scan_legacy(root: str | Path) -> LegacyManifest:
    """Valida todas as fontes e devolve um manifesto reproduzível, sem escrever."""
    root = Path(root).resolve()
    runs_path = root / "runs.sqlite"
    artifacts_path = root / "artifacts.sqlite"
    for required in (runs_path, artifacts_path):
        if not required.is_file():
            raise ValueError(f"fonte legada ausente: {required.name}")

    with sqlite3.connect(runs_path) as connection:
        checkpoints = connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
        writes = connection.execute("SELECT count(*) FROM writes").fetchone()[0]
        runs = connection.execute(
            "SELECT count(DISTINCT thread_id) FROM checkpoints"
        ).fetchone()[0]

    with sqlite3.connect(artifacts_path) as connection:
        rows = connection.execute(
            """
            SELECT id, run_id, item_id, creator_id, kind, storage_backend,
                   storage_key, content_type, size_bytes, sha256, source_uri,
                   retention_class, expires_at, meta_json
            FROM artifacts
            ORDER BY storage_key
            """
        ).fetchall()

    artifact_entries: list[LegacyArtifact] = []
    for row in rows:
        path = _artifact_path(root, str(row[6]), row[7])
        data_digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                data_digest.update(chunk)
        checksum = data_digest.hexdigest()
        if row[8] is not None and int(row[8]) != size:
            raise ValueError(f"tamanho divergente para artifact {row[6]}")
        if row[9] and str(row[9]) != checksum:
            raise ValueError(f"SHA-256 divergente para artifact {row[6]}")
        artifact_entries.append(LegacyArtifact(row, path, size, checksum))

    creators = _json_object(root / "creators.json")
    creator_assets = _creator_assets(root, creators)
    prompts = _json_object(root / "prompts.json")
    feedback = _json_object(root / "feedback.json")
    last_used = prompts.get("last_used") if isinstance(prompts.get("last_used"), dict) else {}

    digest = hashlib.sha256()
    for label, path in (
        ("runs.sqlite", runs_path),
        ("artifacts.sqlite", artifacts_path),
        ("creators.json", root / "creators.json"),
        ("prompts.json", root / "prompts.json"),
        ("feedback.json", root / "feedback.json"),
    ):
        if path.exists():
            _digest_file(digest, label, path)
    for artifact in artifact_entries:
        digest.update(str(artifact.row[6]).encode("utf-8"))
        digest.update(bytes.fromhex(artifact.sha256))
    for asset in creator_assets:
        digest.update(asset.creator_key.encode("utf-8"))
        digest.update(asset.field.encode("utf-8"))
        digest.update(bytes.fromhex(asset.sha256))

    return LegacyManifest(
        root=root,
        checksum=digest.hexdigest(),
        counts={
            "runs": int(runs),
            "checkpoints": int(checkpoints),
            "writes": int(writes),
            "artifacts": len(artifact_entries),
            "artifact_bytes": sum(entry.size_bytes for entry in artifact_entries),
            "creators": len(creators),
            "creator_assets": len(creator_assets),
            "prompt_last_used": len(last_used),
            "feedback": len(feedback),
        },
        artifacts=tuple(artifact_entries),
        creator_assets=creator_assets,
    )


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


async def _begin_import(
    database: Database,
    tenant: Any,
    manifest: LegacyManifest,
    source_id: str,
) -> bool:
    async with database.connection(tenant) as connection:
        cursor = await connection.execute(
            """
            SELECT checksum, status
            FROM legacy_import_batches
            WHERE organization_id = %s AND source_id = %s
            """,
            (tenant.organization_id, source_id),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            if existing[0] != manifest.checksum:
                raise LegacyImportDriftError(
                    f"origem {source_id!r} mudou após o primeiro registro"
                )
            if existing[1] == "applied":
                return False
        await connection.execute(
            """
            INSERT INTO legacy_import_batches (
                organization_id, source_id, checksum, status, manifest
            )
            VALUES (%s, %s, %s, 'pending', %s)
            ON CONFLICT (organization_id, source_id) DO UPDATE
            SET status = 'pending', error = NULL, applied_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                tenant.organization_id,
                source_id,
                manifest.checksum,
                Jsonb({"counts": manifest.counts}),
            ),
        )
    return True


async def _mark_import_failed(
    database: Database,
    tenant: Any,
    source_id: str,
    error: Exception,
) -> None:
    async with database.connection(tenant) as connection:
        await connection.execute(
            """
            UPDATE legacy_import_batches
            SET status = 'failed', error = %s, updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = %s AND source_id = %s
            """,
            (str(error)[:2000], tenant.organization_id, source_id),
        )


async def _import_checkpoints_and_runs(
    manifest: LegacyManifest,
    *,
    database: Database,
    database_url: str,
    tenant: Any,
) -> None:
    async with (
        open_sqlite_checkpointer(manifest.root / "runs.sqlite") as source,
        open_tenant_postgres_checkpointer(
            database_url,
            str(tenant.organization_id),
        ) as target,
    ):
        async for checkpoint in source.alist(None):
            configurable = checkpoint.config["configurable"]
            parent = checkpoint.parent_config or {
                "configurable": {
                    "thread_id": configurable["thread_id"],
                    "checkpoint_ns": configurable.get("checkpoint_ns", ""),
                }
            }
            await target.aput(
                parent,
                checkpoint.checkpoint,
                checkpoint.metadata,
                checkpoint.checkpoint.get("channel_versions", {}),
            )
            writes_by_task: dict[str, list[tuple[str, Any]]] = defaultdict(list)
            for task_id, channel, value in checkpoint.pending_writes or []:
                writes_by_task[str(task_id)].append((str(channel), value))
            for task_id, writes in writes_by_task.items():
                await target.aput_writes(checkpoint.config, writes, task_id)

        with sqlite3.connect(manifest.root / "runs.sqlite") as connection:
            run_ids = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT thread_id
                    FROM checkpoints
                    WHERE checkpoint_ns = ''
                    ORDER BY thread_id
                    """
                )
            ]
        runs = PostgresRunRepository(database, tenant)
        for run_id in run_ids:
            latest = await source.aget_tuple(
                {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
            )
            assert latest is not None
            state = _plain(latest.checkpoint.get("channel_values", {}))
            config = state.get("config") if isinstance(state.get("config"), dict) else {}
            results = state.get("results") if isinstance(state.get("results"), list) else []
            items = [item for item in results if isinstance(item, dict) and item.get("id")]
            await runs.start(
                run_id,
                offer=str(config.get("offer") or "legacy import"),
                platform=str(config.get("platform") or "tiktok"),
                batch_size=int(config.get("batch_size") or max(1, len(items))),
            )
            await runs.save(
                run_id,
                phase="done" if items else "running",
                state=state,
                summary=_plain(runner.summarize({**state, "run_id": run_id})),
                items=items,
            )


async def _import_json_stores(
    manifest: LegacyManifest,
    *,
    database: Database,
    tenant: Any,
    creator_asset_uris: dict[tuple[str, str], str],
) -> None:
    creators = _json_object(manifest.root / "creators.json")
    creator_repository = PostgresCreatorRepository(database, tenant)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for creator_key, entry in sorted(
        (
            (str(key), value)
            for key, value in creators.items()
            if isinstance(value, dict)
        ),
        key=lambda item: item[1].get("_idx", 0),
    ):
        imported = dict(entry)
        for field in ("image", "voice_preview"):
            uri = creator_asset_uris.get((creator_key, field))
            if uri is not None:
                imported[f"{field}_uri"] = uri
        grouped[str(entry.get("run_id") or "legacy")].append(imported)
    for run_id, entries in grouped.items():
        normalized = [{**entry, "id": str(entry.get("creator_id") or "")} for entry in entries]
        await creator_repository.record_creators(
            run_id,
            normalized,
            approved_ids=[
                str(entry.get("creator_id") or "")
                for entry in entries
                if entry.get("status", "approved") == "approved"
            ],
            creator_prompt=next((entry.get("creator_prompt") for entry in entries if entry.get("creator_prompt")), None),
            video_prompt=next((entry.get("video_prompt") for entry in entries if entry.get("video_prompt")), None),
            offer=next((entry.get("offer") for entry in entries if entry.get("offer")), None),
        )

    prompts = _json_object(manifest.root / "prompts.json")
    prompt_repository = PostgresPromptRepository(database, tenant)
    templates = prompts.get("templates") if isinstance(prompts.get("templates"), dict) else {}
    for template in sorted(
        (value for value in templates.values() if isinstance(value, dict)),
        key=lambda value: value.get("_idx", 0),
    ):
        await prompt_repository.save_template(
            kind=template.get("kind"),
            title=template.get("title"),
            text=template.get("text"),
            desc=template.get("desc", ""),
        )
    last_used = prompts.get("last_used") if isinstance(prompts.get("last_used"), dict) else {}
    await prompt_repository.record_last_used(
        creator_prompt=last_used.get("creator"),
        video_prompt=last_used.get("video"),
    )

    feedback = _json_object(manifest.root / "feedback.json")
    feedback_repository = PostgresFeedbackRepository(database, tenant)
    for run_id, summary in sorted(
        feedback.items(),
        key=lambda item: item[1].get("_idx", 0) if isinstance(item[1], dict) else 0,
    ):
        if isinstance(summary, dict):
            await feedback_repository.save_feedback(
                run_id,
                {key: value for key, value in summary.items() if key != "_idx"},
            )


async def _import_creator_assets(
    manifest: LegacyManifest,
    *,
    database: Database,
    tenant: Any,
    storage: MediaStorage,
    source_id: str,
) -> dict[tuple[str, str], str]:
    imported: dict[tuple[str, str], str] = {}
    source_key = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
    for asset in manifest.creator_assets:
        creator_key = hashlib.sha256(
            asset.creator_key.encode("utf-8")
        ).hexdigest()[:24]
        stored = await storage.put_bytes(
            asset.data,
            key_base=(
                f"tenants/{tenant.organization_id}/legacy/{source_key}/"
                f"creators/{creator_key}/{asset.field}"
            ),
            content_type=asset.content_type,
        )
        if stored.size_bytes != len(asset.data) or stored.sha256 != asset.sha256:
            raise ValueError(
                f"storage divergiu para asset de creator {asset.creator_key}:{asset.field}"
            )
        imported[(asset.creator_key, asset.field)] = stored.uri
        async with database.connection(tenant) as connection:
            await connection.execute(
                """
                INSERT INTO legacy_import_entries (
                    organization_id, source_id, kind, source_key,
                    checksum, size_bytes, status
                )
                VALUES (%s, %s, 'creator_asset', %s, %s, %s, 'applied')
                ON CONFLICT (organization_id, source_id, kind, source_key) DO NOTHING
                """,
                (
                    tenant.organization_id,
                    source_id,
                    f"{asset.creator_key}:{asset.field}",
                    asset.sha256,
                    len(asset.data),
                ),
            )
    return imported


async def _import_artifacts(
    manifest: LegacyManifest,
    *,
    database: Database,
    tenant: Any,
    storage: MediaStorage,
    source_id: str,
) -> None:
    repository = PostgresArtifactRepository(database, tenant)
    for artifact in manifest.artifacts:
        row = artifact.row
        content_type = str(row[7] or "application/octet-stream")
        target_key = f"tenants/{tenant.organization_id}/{row[6]}"
        suffix = Path(target_key).suffix
        key_base = target_key[: -len(suffix)] if suffix else target_key
        stored = await storage.put_bytes(
            artifact.path.read_bytes(),
            key_base=key_base,
            content_type=content_type,
        )
        if stored.size_bytes != artifact.size_bytes or stored.sha256 != artifact.sha256:
            raise ValueError(f"storage divergiu para artifact {row[6]}")
        meta = json.loads(row[13]) if row[13] else {}
        meta.update({"legacy_artifact_id": row[0], "legacy_storage_key": row[6]})
        await repository.record(
            ArtifactRecord(
                run_id=row[1],
                item_id=row[2],
                creator_id=row[3],
                kind=row[4],
                storage_backend=stored.backend,
                storage_key=stored.key,
                content_type=stored.content_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                source_uri=row[10],
                retention_class=row[11],
                expires_at=row[12],
                meta=meta,
            )
        )
        async with database.connection(tenant) as connection:
            await connection.execute(
                """
                INSERT INTO legacy_import_entries (
                    organization_id, source_id, kind, source_key,
                    checksum, size_bytes, status
                )
                VALUES (%s, %s, 'artifact', %s, %s, %s, 'applied')
                ON CONFLICT (organization_id, source_id, kind, source_key) DO NOTHING
                """,
                (
                    tenant.organization_id,
                    source_id,
                    row[6],
                    artifact.sha256,
                    artifact.size_bytes,
                ),
            )


@asynccontextmanager
async def _import_lock(
    database: Database,
    tenant: Any,
    source_id: str,
):
    lock_name = f"{tenant.organization_id}:{source_id}"
    async with database.connection(tenant) as connection:
        await connection.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (lock_name,),
        )
        try:
            yield
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (lock_name,),
            )


async def _apply_for_tenant(
    manifest: LegacyManifest,
    *,
    database: Database,
    database_url: str,
    storage: MediaStorage,
    source_id: str,
    tenant: Any,
) -> LegacyImportResult:
    if not await _begin_import(database, tenant, manifest, source_id):
        return LegacyImportResult("noop", manifest.checksum, manifest.counts)

    try:
        await _import_checkpoints_and_runs(
            manifest,
            database=database,
            database_url=database_url,
            tenant=tenant,
        )
        creator_asset_uris = await _import_creator_assets(
            manifest,
            database=database,
            tenant=tenant,
            storage=storage,
            source_id=source_id,
        )
        await _import_json_stores(
            manifest,
            database=database,
            tenant=tenant,
            creator_asset_uris=creator_asset_uris,
        )
        await _import_artifacts(
            manifest,
            database=database,
            tenant=tenant,
            storage=storage,
            source_id=source_id,
        )
    except Exception as exc:
        await _mark_import_failed(database, tenant, source_id, exc)
        raise
    async with database.connection(tenant) as connection:
        await connection.execute(
            """
            UPDATE legacy_import_batches
            SET status = 'applied', error = NULL, applied_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE organization_id = %s AND source_id = %s
            """,
            (tenant.organization_id, source_id),
        )
    return LegacyImportResult("applied", manifest.checksum, manifest.counts)


async def apply_legacy(
    manifest: LegacyManifest,
    *,
    database: Database,
    database_url: str,
    storage: MediaStorage,
    source_id: str,
) -> LegacyImportResult:
    """Aplica um manifesto uma vez; mesma origem alterada é bloqueada."""
    identity = TenantIdentity.from_env()
    tenant = await database.ensure_tenant(identity)
    async with _import_lock(database, tenant, source_id):
        return await _apply_for_tenant(
            manifest,
            database=database,
            database_url=database_url,
            storage=storage,
            source_id=source_id,
            tenant=tenant,
        )
