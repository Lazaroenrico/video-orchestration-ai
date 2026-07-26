"""CLI do orquestrador: run / status / resume / list."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import click
from dotenv import load_dotenv

from orchestrator import runner
from orchestrator.config import (
    default_artifacts_db_path,
    default_db_path,
    default_media_path,
    default_videos_path,
    load_agent_catalog,
    load_pipeline,
    load_providers,
)
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.logging_config import configure_logging
from orchestrator.operations import PostgresOperations
from orchestrator.legacy_import import apply_legacy, scan_legacy
from orchestrator.db import (
    MEMBERSHIP_ROLES,
    Database,
    TenantIdentity,
    create_organization,
    grant_membership,
    provision_runtime_role,
    revoke_membership,
    upgrade_database,
)
from orchestrator.db.artifacts import PostgresArtifactRepository
from orchestrator.storage.db import ArtifactDB
from orchestrator.storage.factory import build_media_storage
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


def _do_run(*, batch, offer, platform, run_id, config_dir, db, feedback_store):
    """Executa a pipeline uma vez e imprime o relatório. Compartilhado por `run`/`runner`."""
    pipeline = load_pipeline(config_dir)
    providers = load_providers(config_dir)
    agent_catalog = load_agent_catalog(config_dir)
    db_path = db or default_db_path()
    rid, out = asyncio.run(
        runner.run_pipeline(
            pipeline, providers, db_path=db_path, run_id=run_id,
            batch=batch, offer=offer, platform=platform, feedback_store=feedback_store,
            agent_catalog=agent_catalog,
        )
    )
    click.echo(runner.format_report({**out, "run_id": rid}))


@cli.command()
@click.option("--batch", type=int, default=None, help="Tamanho do batch (default: pipeline.yaml).")
@click.option("--offer", default="demo offer", help="Oferta/produto base dos conceitos.")
@click.option("--platform", default="tiktok", help="Plataforma alvo (calibra script/montagem).")
@click.option("--run-id", default=None, help="Id do run (default: gerado).")
@click.option("--dry-run/--no-dry-run", default=True, help="v1 é sempre mock; flag reservada.")
@click.option("--config-dir", default=None, help="Diretório de configs (default: ./config).")
@click.option("--db", default=None, help="Arquivo sqlite de estado (default: .orchestrator/runs.sqlite).")
@click.option("--feedback-store", default=None, help="JSON p/ persistir o feedback (Step 10) e fechar o loop.")
def run(batch, offer, platform, run_id, dry_run, config_dir, db, feedback_store):
    """Roda a pipeline mock ponta a ponta."""
    _do_run(
        batch=batch, offer=offer, platform=platform, run_id=run_id,
        config_dir=config_dir, db=db, feedback_store=feedback_store,
    )


@cli.command()
@click.option("--cycles", type=int, required=True, help="Número de ciclos encadeados a rodar.")
@click.option("--batch", type=int, default=None, help="Tamanho do batch (default: pipeline.yaml).")
@click.option("--offer", default="demo offer", help="Oferta/produto base dos conceitos.")
@click.option("--platform", default="tiktok", help="Plataforma alvo (calibra script/montagem).")
@click.option("--run-id-prefix", default=None, help="Prefixo dos run_ids (default: gerado).")
@click.option("--config-dir", default=None, help="Diretório de configs (default: ./config).")
@click.option("--db", default=None, help="Arquivo sqlite de estado (default: .orchestrator/runs.sqlite).")
@click.option("--feedback-store", required=True, help="JSON do feedback — obrigatório p/ encadear os ciclos.")
def loop(cycles, batch, offer, platform, run_id_prefix, config_dir, db, feedback_store):
    """Roda N ciclos encadeados; cada ciclo lê o feedback do anterior (close-the-loop)."""
    pipeline = load_pipeline(config_dir)
    providers = load_providers(config_dir)
    agent_catalog = load_agent_catalog(config_dir)
    db_path = db or default_db_path()
    results = asyncio.run(
        runner.run_cycles(
            pipeline, providers, db_path=db_path, cycles=cycles,
            feedback_store=feedback_store, batch=batch, offer=offer,
            platform=platform, run_id_prefix=run_id_prefix,
            agent_catalog=agent_catalog,
        )
    )
    for i, (rid, out) in enumerate(results, 1):
        click.echo(f"=== ciclo {i}/{cycles} ===")
        click.echo(runner.format_report({**out, "run_id": rid}))


@cli.command()
@click.argument("run_id")
@click.option("--config-dir", default=None)
@click.option("--db", default=None)
def status(run_id, config_dir, db):
    """Mostra o estado de um run a partir do checkpoint."""
    pipeline = load_pipeline(config_dir)
    db_path = db or default_db_path()
    state = asyncio.run(runner.get_status(pipeline, db_path=db_path, run_id=run_id))
    if state is None:
        click.echo(f"run {run_id}: não encontrado")
        raise SystemExit(1)
    click.echo(runner.format_report({**state, "run_id": run_id}))


@cli.command()
@click.argument("run_id")
@click.option("--platform", default="tiktok")
@click.option("--config-dir", default=None)
@click.option("--db", default=None)
@click.option("--feedback-store", default=None, help="JSON p/ persistir o feedback (Step 10).")
def resume(run_id, platform, config_dir, db, feedback_store):
    """Retoma um run interrompido (mesmo thread_id)."""
    pipeline = load_pipeline(config_dir)
    providers = load_providers(config_dir)
    agent_catalog = load_agent_catalog(config_dir)
    db_path = db or default_db_path()
    rid, out = asyncio.run(
        runner.resume_pipeline(
            pipeline, providers, db_path=db_path, run_id=run_id,
            platform=platform, feedback_store=feedback_store,
            agent_catalog=agent_catalog,
        )
    )
    click.echo(runner.format_report({**out, "run_id": rid}))


@cli.command(name="list")
@click.option("--db", default=None)
def list_runs(db):
    """Lista os run_ids conhecidos."""
    db_path = db or default_db_path()
    runs = runner.list_runs(db_path)
    if not runs:
        click.echo("nenhum run encontrado")
        return
    for r in runs:
        click.echo(r)


@cli.command(name="runner")
@click.option("--batch", type=int, default=None, help="Tamanho do batch (default: pipeline.yaml).")
@click.option("--offer", default="demo offer", help="Oferta/produto base dos conceitos.")
@click.option("--platform", default="tiktok", help="Plataforma alvo (calibra script/montagem).")
@click.option("--run-id", default=None, help="Id do run (default: gerado).")
@click.option("--config-dir", default=None, help="Diretório de configs (default: ./config).")
@click.option("--db", default=None, help="Arquivo sqlite de estado (default: .orchestrator/runs.sqlite).")
@click.option("--feedback-store", default=None, help="JSON p/ persistir o feedback (Step 10).")
@click.option("--once", is_flag=True, help="Consome no máximo um job PostgreSQL.")
@click.option(
    "--worker-id",
    default=lambda: os.environ.get("HOSTNAME", "runner"),
    show_default="HOSTNAME ou runner",
)
def runner_command(
    batch,
    offer,
    platform,
    run_id,
    config_dir,
    db,
    feedback_store,
    once,
    worker_id,
):
    """Executa a pipeline (papel de Runner do container OCI).

    Com ``--once``, consome um job durável; sem a flag, preserva o one-shot local.
    """
    if once:
        worked = asyncio.run(run_worker_once(worker_id=worker_id))
        click.echo("job processado" if worked else "fila vazia")
        return
    _do_run(
        batch=batch, offer=offer, platform=platform, run_id=run_id,
        config_dir=config_dir, db=db, feedback_store=feedback_store,
    )


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
    database_url = migration_database_url or legacy_database_url
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


if __name__ == "__main__":  # pragma: no cover - entrypoint executado só via `python -m`
    cli()
