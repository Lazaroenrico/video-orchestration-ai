"""Smoke tests da CLI (run/status/resume/list)."""
import os
import shutil

from click.testing import CliRunner

from orchestrator.cli import cli


CLI_OFFLINE_ENV = {"LANGSMITH_TRACING": "false"}


def _invoke(cr: CliRunner, args):
    return cr.invoke(cli, args, env=CLI_OFFLINE_ENV)


def test_serve_command_invokes_uvicorn(monkeypatch):
    """`orchestrator serve` configura logging e delega para uvicorn.run (patched)."""
    import uvicorn

    calls: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.setdefault("kwargs", k))

    result = CliRunner().invoke(cli, ["serve", "--port", "9123"], env=CLI_OFFLINE_ENV)

    assert result.exit_code == 0, result.output
    assert calls["kwargs"]["port"] == 9123
    assert calls["kwargs"]["host"] == "0.0.0.0"


def test_api_command_invokes_uvicorn(monkeypatch):
    """`orchestrator api` sobe o mesmo app FastAPI que `serve` (papel de API da ADR-D36)."""
    import uvicorn

    calls: dict = {}

    def fake_run(*a, **k):
        calls["import_string"] = a[0] if a else None
        calls["kwargs"] = k

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(cli, ["api", "--port", "9200"], env=CLI_OFFLINE_ENV)

    assert result.exit_code == 0, result.output
    assert calls["import_string"] == "orchestrator.web.server:app"
    assert calls["kwargs"]["port"] == 9200
    assert calls["kwargs"]["host"] == "0.0.0.0"


def test_cli_runner_command_runs_pipeline(tmp_path):
    """`orchestrator runner` reusa o caminho de `run` (Fase 1 one-shot)."""
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    cfg = _mock_config_dir(tmp_path)

    res = _invoke(cr, ["runner", "--batch", "3", "--run-id", "rn-1", "--db", db, "--config-dir", cfg])
    assert res.exit_code == 0, res.output
    assert "produzidos : 3" in res.output

    res2 = _invoke(cr, ["list", "--db", db])
    assert "rn-1" in res2.output


def test_cli_migrate_materializes_state_and_is_idempotent(tmp_path):
    """`orchestrator migrate` cria checkpointer + ArtifactDB + dirs de mídia; roda 2x sem falhar."""
    cr = CliRunner()
    db = tmp_path / "runs.sqlite"
    art = tmp_path / "artifacts.sqlite"
    media = tmp_path / "media"
    videos = tmp_path / "videos"
    env = {**CLI_OFFLINE_ENV, "ORCH_MEDIA": str(media), "ORCH_VIDEOS": str(videos)}

    res = cr.invoke(cli, ["migrate", "--db", str(db), "--artifacts-db", str(art)], env=env)
    assert res.exit_code == 0, res.output
    assert db.exists() and art.exists()
    assert media.is_dir() and videos.is_dir()
    assert "estado materializado" in res.output

    res2 = cr.invoke(cli, ["migrate", "--db", str(db), "--artifacts-db", str(art)], env=env)
    assert res2.exit_code == 0, res2.output


def _mock_config_dir(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy("config/pipeline.yaml", cfg / "pipeline.yaml")
    (cfg / "providers.yaml").write_text(
        "adapters:\n"
        "  llm: mock\n"
        "  creator: mock\n"
        "  video: mock\n"
        "  qc: mock\n"
        "  assembly: mock\n",
        encoding="utf-8",
    )
    return str(cfg)


def test_cli_run_status_list(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    cfg = _mock_config_dir(tmp_path)

    res = _invoke(cr, ["run", "--batch", "6", "--run-id", "cli-1", "--db", db, "--config-dir", cfg])
    assert res.exit_code == 0, res.output
    assert "produzidos : 6" in res.output

    res2 = _invoke(cr, ["status", "cli-1", "--db", db, "--config-dir", cfg])
    assert res2.exit_code == 0, res2.output
    assert "cli-1" in res2.output
    assert "produzidos : 6" in res2.output

    res3 = _invoke(cr, ["list", "--db", db])
    assert res3.exit_code == 0
    assert "cli-1" in res3.output


def test_cli_status_unknown_run_fails(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    cfg = _mock_config_dir(tmp_path)
    # cria o arquivo com um run qualquer primeiro
    _invoke(cr, ["run", "--batch", "2", "--run-id", "exists", "--db", db, "--config-dir", cfg])
    res = _invoke(cr, ["status", "nope", "--db", db, "--config-dir", cfg])
    assert res.exit_code != 0
    assert "não encontrado" in res.output


def test_cli_resume_smoke(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    cfg = _mock_config_dir(tmp_path)
    _invoke(cr, ["run", "--batch", "4", "--run-id", "r1", "--db", db, "--config-dir", cfg])
    res = _invoke(cr, ["resume", "r1", "--db", db, "--config-dir", cfg])
    assert res.exit_code == 0, res.output
    assert "run r1" in res.output


def test_cli_loop_runs_n_cycles(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    store = str(tmp_path / "feedback.json")
    cfg = _mock_config_dir(tmp_path)
    res = _invoke(cr, [
        "loop", "--cycles", "2", "--batch", "6", "--run-id-prefix", "L",
        "--db", db, "--feedback-store", store, "--config-dir", cfg,
    ])
    assert res.exit_code == 0, res.output
    assert "ciclo 1/2" in res.output
    assert "ciclo 2/2" in res.output
    # ambos os ciclos foram persistidos no store e checkpointados
    res2 = _invoke(cr, ["list", "--db", db])
    assert "L-c1" in res2.output
    assert "L-c2" in res2.output


def test_cli_loop_requires_feedback_store(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    cfg = _mock_config_dir(tmp_path)
    res = _invoke(cr, [
        "loop", "--cycles", "2", "--db", db, "--config-dir", cfg,
    ])
    assert res.exit_code != 0


def test_cli_loads_dotenv_from_cwd(monkeypatch):
    cr = CliRunner()
    observed = {}

    def fake_list_runs(_db):
        observed["gateway"] = os.environ.get("AI_GATEWAY_API_KEY")
        return []

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr("orchestrator.cli.runner.list_runs", fake_list_runs)

    with cr.isolated_filesystem():
        with open(".env", "w", encoding="utf-8") as f:
            f.write("AI_GATEWAY_API_KEY=from-dotenv\n")

        res = cr.invoke(cli, ["list"])

    assert res.exit_code == 0, res.output
    assert observed["gateway"] == "from-dotenv"


def test_cli_does_not_override_existing_env_with_dotenv(monkeypatch):
    cr = CliRunner()
    observed = {}

    def fake_list_runs(_db):
        observed["gateway"] = os.environ.get("AI_GATEWAY_API_KEY")
        return []

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "already-exported")
    monkeypatch.setattr("orchestrator.cli.runner.list_runs", fake_list_runs)

    with cr.isolated_filesystem():
        with open(".env", "w", encoding="utf-8") as f:
            f.write("AI_GATEWAY_API_KEY=from-dotenv\n")

        res = cr.invoke(cli, ["list"])

    assert res.exit_code == 0, res.output
    assert observed["gateway"] == "already-exported"
