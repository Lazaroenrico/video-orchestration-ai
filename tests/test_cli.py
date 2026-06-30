"""Smoke tests da CLI (run/status/resume/list)."""
import os
import shutil

from click.testing import CliRunner

from orchestrator.cli import cli


CLI_OFFLINE_ENV = {"LANGSMITH_TRACING": "false"}


def _invoke(cr: CliRunner, args):
    return cr.invoke(cli, args, env=CLI_OFFLINE_ENV)


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
        "  assembly: mock\n"
        "  distribution: mock\n",
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
