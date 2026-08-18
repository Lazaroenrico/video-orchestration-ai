"""CLI operacional do orquestrador.

Campanhas são iniciadas e retomadas pela API V2; a CLI mantém somente comandos
operacionais e o consumidor durável ``runner --once``.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import click
from dotenv import load_dotenv

from orchestrator.config import (
    default_artifacts_db_path,
    default_db_path,
    default_media_path,
    default_videos_path,
    load_providers,
)
from orchestrator.db import (
    MEMBERSHIP_ROLES,
    Database,
    PostgresEffectLedger,
    TenantIdentity,
    create_organization,
    grant_membership,
    provision_runtime_role,
    revoke_membership,
    upgrade_database,
)
from orchestrator.db.artifacts import PostgresArtifactRepository
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.legacy_import import apply_legacy, scan_legacy
from orchestrator.logging_config import configure_logging
from orchestrator.operations import PostgresOperations
from orchestrator.sqs_runner import run_sqs_runner
from orchestrator.storage.db import ArtifactDB
from orchestrator.storage.factory import build_media_storage
from orchestrator.storage.migration import BotoObjectStore, migrate_run_objects
from orchestrator.storage.retention import purge_expired
from orchestrator.worker import run_worker_once


@click.group()
def cli() -> None:
    """Orquestrador da pipeline de AI UGC (v1 — mock/dry-run)."""
    load_dotenv(".env", override=False)
    configure_logging()


@cli.group(name="db")
def db_commands() -> None:
    """Administração do PostgreSQL."""


@cli.group(name="ops")
def operations_commands() -> None:
    """Diagnóstico operacional tenant-scoped."""


@cli.group(name="storage")
def storage_commands() -> None:
    """Migração administrada de objetos."""


@storage_commands.command(name="migrate-run")
@click.argument("run_id")
def storage_migrate_run(run_id: str) -> None:
    """Copia um run de R2 para S3 e troca o backend após verificação."""
    source = BotoObjectStore.from_r2_env()
    destination = BotoObjectStore.from_s3_env()

    async def _migrate() -> dict:
        async with Database.from_env() as database:
            tenant = await database.resolve_tenant(TenantIdentity.from_env())
            artifacts = PostgresArtifactRepository(database, tenant)
            return await migrate_run_objects(
                artifacts,
                run_id=run_id,
                source=source,
                destination=destination,
            )

    report = asyncio.run(_migrate())
    click.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))


@operations_commands.command(name="inspect-run")
@click.argument("run_id")
def inspect_run(run_id: str) -> None:
    """Reconstrói um run exclusivamente a partir do estado durável."""

    async def _inspect() -> dict:
        async with Database.from_env() as database:
            tenant = await database.resolve_tenant(TenantIdentity.from_env())
            return await PostgresOperations(database, tenant).inspect_run(run_id)

    try:
        report = asyncio.run(_inspect())
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))


@operations_commands.command(name="maintain")
@click.option("--config-dir", default=None, help="Diretório de providers.yaml.")
def maintain(config_dir: str | None) -> None:
    """Executa purge orientado pelo DB, inventário e health snapshot."""
    storage = build_media_storage(
        load_providers(config_dir),
        root=default_media_path(),
        web_prefix="/media",
    )

    async def _maintain() -> dict:
        now = datetime.now(UTC)
        async with Database.from_env() as database:
            tenant = await database.resolve_tenant(TenantIdentity.from_env())
            artifacts = PostgresArtifactRepository(database, tenant)
            purged = await purge_expired(artifacts, storage, now=now)
            operations = PostgresOperations(database, tenant)
            return {
                "purged": purged,
                "inventory": await operations.object_inventory(storage),
                "health": await operations.health_snapshot(now=now),
            }

    report = asyncio.run(_maintain())
    click.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))


@db_commands.command(name="provision-runtime")
@click.option(
    "--migration-database-url",
    envvar="MIGRATION_DATABASE_URL",
    required=True,
    help="Conexão direta e privilegiada.",
)
def provision_runtime(migration_database_url: str) -> None:
    """Cria/atualiza o papel fixo e restrito da aplicação."""
    password = os.environ.get("ORCHESTRATOR_RUNTIME_PASSWORD", "")
    try:
        provision_runtime_role(migration_database_url, password)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("papel orchestrator_runtime provisionado com RLS obrigatório")


VOICE_QUOTA_BUCKETS = (
    "elevenlabs_voice_design_chars",
    "elevenlabs_voice_slots",
    "elevenlabs_tts_chars",
)


async def _set_provider_quota(provider: str, limit_units: int) -> None:
    async with Database.from_env() as database:
        tenant = await database.resolve_tenant(TenantIdentity.from_env())
        await PostgresEffectLedger(database, tenant).set_quota(
            provider,
            limit_units=limit_units,
        )


@db_commands.command(name="set-provider-quota")
@click.option("--provider", required=True)
@click.option("--limit-units", type=click.IntRange(min=0), required=True)
def set_provider_quota(provider: str, limit_units: int) -> None:
    """Configure a tenant-scoped quota for any paid provider bucket."""
    provider = provider.strip()
    if not provider:
        raise click.ClickException("provider não pode ser vazio")
    asyncio.run(_set_provider_quota(provider, limit_units))
    click.echo(f"quota {provider} configurada em {limit_units} unidades")


@db_commands.command(name="set-voice-quota")
@click.option("--bucket", type=click.Choice(VOICE_QUOTA_BUCKETS), required=True)
@click.option("--limit-units", type=click.IntRange(min=0), required=True)
def set_voice_quota(bucket: str, limit_units: int) -> None:
    """Configure a tenant-scoped operational quota for direct ElevenLabs calls."""

    asyncio.run(_set_provider_quota(bucket, limit_units))
    click.echo(f"quota {bucket} configurada em {limit_units} unidades")


@db_commands.command(name="org-create")
@click.option(
    "--migration-database-url",
    envvar="MIGRATION_DATABASE_URL",
    required=True,
    help="Conexão direta e privilegiada.",
)
@click.option("--slug", required=True)
@click.option("--name", required=True)
def organization_create(
    migration_database_url: str,
    slug: str,
    name: str,
) -> None:
    """Cria ou atualiza uma organização explicitamente."""
    create_organization(migration_database_url, slug=slug, name=name)
    click.echo(f"organização {slug!r} provisionada")


@db_commands.command(name="membership-grant")
@click.option(
    "--migration-database-url",
    envvar="MIGRATION_DATABASE_URL",
    required=True,
    help="Conexão direta e privilegiada.",
)
@click.option("--organization-slug", required=True)
@click.option("--user-subject", required=True)
@click.option("--role", type=click.Choice(MEMBERSHIP_ROLES), required=True)
def membership_grant(
    migration_database_url: str,
    organization_slug: str,
    user_subject: str,
    role: str,
) -> None:
    """Concede membership; nunca é executado pela API pública."""
    try:
        grant_membership(
            migration_database_url,
            organization_slug=organization_slug,
            user_subject=user_subject,
            role=role,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"membership {role!r} concedida a {user_subject!r} em {organization_slug!r}"
    )


@db_commands.command(name="membership-revoke")
@click.option(
    "--migration-database-url",
    envvar="MIGRATION_DATABASE_URL",
    required=True,
    help="Conexão direta e privilegiada.",
)
@click.option("--organization-slug", required=True)
@click.option("--user-subject", required=True)
def membership_revoke(
    migration_database_url: str,
    organization_slug: str,
    user_subject: str,
) -> None:
    """Revoga uma membership mantendo o usuário para auditoria."""
    removed = revoke_membership(
        migration_database_url,
        organization_slug=organization_slug,
        user_subject=user_subject,
    )
    suffix = "" if removed else " (já ausente)"
    click.echo(
        f"membership de {user_subject!r} em {organization_slug!r} revogada{suffix}"
    )


@cli.command(name="import-legacy")
@click.option(
    "--legacy-root",
    default=".orchestrator",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Diretório contendo runs.sqlite, artifacts.sqlite e stores JSON.",
)
@click.option("--source-id", default="legacy-local", show_default=True)
@click.option("--apply", is_flag=True, default=False, help="Confirma a escrita no destino.")
@click.option("--config-dir", default=None, help="Diretório de providers.yaml.")
def import_legacy(
    legacy_root: Path,
    source_id: str,
    apply: bool,
    config_dir: str | None,
) -> None:
    """Inventaria o legado; só escreve com --apply explícito."""
    manifest = scan_legacy(legacy_root)
    mode = "dry-run"
    if apply:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise click.ClickException("DATABASE_URL é obrigatória com --apply")
        try:
            storage = build_media_storage(
                load_providers(config_dir),
                root=default_media_path(),
                web_prefix="/media",
            )

            async def _apply() -> str:
                async with Database.from_env() as database:
                    result = await apply_legacy(
                        manifest,
                        database=database,
                        database_url=database_url,
                        storage=storage,
                        source_id=source_id,
                    )
                return result.mode

            mode = asyncio.run(_apply())
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(
        json.dumps(
            {
                "mode": mode,
                "source_id": source_id,
                "checksum": manifest.checksum,
                "counts": manifest.counts,
            },
            sort_keys=True,
        )
    )


@cli.command(name="runner")
@click.option("--once", is_flag=True, help="Consome no máximo um job PostgreSQL.")
@click.option(
    "--worker-id",
    default=lambda: os.environ.get("HOSTNAME", "runner"),
    show_default="HOSTNAME ou runner",
)
def runner_command(
    once,
    worker_id,
):
    """Consome exclusivamente um job durável."""
    if not once:
        raise click.ClickException("runner exige --once; campanhas são iniciadas pela API V2")
    worked = asyncio.run(run_worker_once(worker_id=worker_id))
    click.echo("job processado" if worked else "fila vazia")


DEFAULT_DEV_QUOTAS = {
    "openai_image_units": 50,
    "elevenlabs_voice_design_chars": 100000,
    "elevenlabs_voice_slots": 50,
    "elevenlabs_tts_chars": 200000,
    "replicate_video_seconds": 300,
}


async def _seed_default_dev_quotas() -> None:
    async with Database.from_env() as database:
        tenant = await database.resolve_tenant(TenantIdentity.from_env())
        ledger = PostgresEffectLedger(database, tenant)
        for bucket, default_limit in DEFAULT_DEV_QUOTAS.items():
            try:
                row = await database.fetch_one(
                    "SELECT limit_units FROM provider_quotas WHERE organization_id = $1 AND provider = $2",
                    tenant.organization_id,
                    bucket,
                )
                if row is None:
                    await ledger.set_quota(bucket, limit_units=default_limit)
            except Exception:
                pass


@cli.command()
@click.option("--db", default=None, help="Checkpointer sqlite (default: .orchestrator/runs.sqlite).")
@click.option("--artifacts-db", default=None, help="ArtifactDB sqlite (default: .orchestrator/artifacts.sqlite).")
@click.option(
    "--migration-database-url",
    envvar="MIGRATION_DATABASE_URL",
    default=None,
    help="Conexão PostgreSQL direta e privilegiada para Alembic.",
)
@click.option(
    "--database-url",
    "legacy_database_url",
    default=None,
    help="Alias explícito legado de --migration-database-url.",
)
def migrate(db, artifacts_db, migration_database_url, legacy_database_url):
    """Materializa o estado local (papel de `migrate` do container OCI).

    Fase 1 da ADR-D36: cria o schema do checkpointer e do ArtifactDB e os diretórios de
    mídia. Idempotente. Substituído por migrações SQL do PostgreSQL na Fase 2.
    """
    # O alias explícito de CLI precisa vencer a URL carregada do ambiente/.env.
    database_url = legacy_database_url or migration_database_url
    if database_url is None and os.environ.get("ORCH_ENV", "local") == "local":
        database_url = os.environ.get("DATABASE_URL")
    if (
        database_url is None
        and os.environ.get("ORCH_ENV", "local") in {"staging", "production"}
    ):
        raise click.ClickException(
            "MIGRATION_DATABASE_URL é obrigatória em staging/production"
        )
    if database_url:
        upgrade_database(database_url)
        if os.environ.get("ORCH_ENV", "local") == "local":
            try:
                asyncio.run(_seed_default_dev_quotas())
            except Exception:
                pass
        click.echo("PostgreSQL migrado: revision=head")
        return

    db_path = db or default_db_path()
    artifacts_path = artifacts_db or default_artifacts_db_path()
    for directory in (default_media_path(), default_videos_path()):
        directory.mkdir(parents=True, exist_ok=True)
    ArtifactDB(artifacts_path).setup()

    async def _prepare_checkpointer() -> None:
        async with open_checkpointer(db_path):
            pass  # setup() materializa o schema ao abrir

    asyncio.run(_prepare_checkpointer())
    click.echo(f"estado materializado: checkpointer={db_path} artifacts={artifacts_path}")


def _run_uvicorn(host, port, reload, *, application: str = "orchestrator.web.server:app"):
    """Sobe o servidor web (dashboard + API + SSE). Compartilhado por `api`/`serve`."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - uvicorn faz parte das deps [web] instaladas
        raise click.ClickException(
            "uvicorn não instalado. Execute: uv pip install -e '.[web]'"
        )
    load_dotenv(".env", override=False)
    configure_logging()
    click.echo(f"Dashboard disponível em: http://localhost:{port}/")
    uvicorn.run(
        application,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@cli.command()
@click.option("--host", default="0.0.0.0", help="Host de escuta.")
@click.option("--port", default=8000, type=int, help="Porta de escuta.")
@click.option("--reload", is_flag=True, default=False, help="Hot-reload (dev).")
def api(host, port, reload):
    """Inicia a API/dashboard web (papel de API do container OCI)."""
    _run_uvicorn(host, port, reload)


@cli.command()
@click.option("--host", default="0.0.0.0", help="Host de escuta.")
@click.option("--port", default=8000, type=int, help="Porta de escuta.")
@click.option("--reload", is_flag=True, default=False, help="Hot-reload (dev).")
def serve(host, port, reload):
    """Alias retrocompatível de `api`."""
    _run_uvicorn(host, port, reload)


@cli.command(name="runner-service")
@click.option("--host", default="0.0.0.0", help="Host de escuta.")
@click.option("--port", default=8000, type=int, help="Porta de escuta.")
def runner_service(host: str, port: int) -> None:
    """Inicia o launcher HTTP interno do Runner Container."""
    _run_uvicorn(
        host,
        port,
        False,
        application="orchestrator.runner_service:app",
    )


@cli.command(name="sqs-runner")
@click.option("--worker-id", default="ecs-runner", show_default=True)
@click.option(
    "--cycles",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="0 roda continuamente; valor positivo é smoke/teste.",
)
def sqs_runner(worker_id: str, cycles: int) -> None:
    """Consome wake-ups SQS e reivindica o trabalho canônico no PostgreSQL."""
    bounded_cycles = cycles or None
    result = asyncio.run(
        run_sqs_runner(
            worker_id=worker_id,
            cycles=bounded_cycles,
        )
    )
    if bounded_cycles is not None:
        click.echo(json.dumps(result, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - entrypoint executado só via `python -m`
    cli()
