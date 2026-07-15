"""CLI do orquestrador: run / status / resume / list."""
from __future__ import annotations

import asyncio

import click
from dotenv import load_dotenv

from orchestrator import runner
from orchestrator.config import (
    default_db_path,
    load_agent_catalog,
    load_pipeline,
    load_providers,
)
from orchestrator.logging_config import configure_logging


@click.group()
def cli() -> None:
    """Orquestrador da pipeline de AI UGC (v1 — mock/dry-run)."""
    load_dotenv(".env", override=False)
    configure_logging()


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


@cli.command()
@click.option("--host", default="0.0.0.0", help="Host de escuta.")
@click.option("--port", default=8000, type=int, help="Porta de escuta.")
@click.option("--reload", is_flag=True, default=False, help="Hot-reload (dev).")
def serve(host, port, reload):
    """Inicia o servidor web com dashboard em tempo real (SSE + streaming LLM)."""
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
        "orchestrator.web.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover - entrypoint executado só via `python -m`
    cli()
