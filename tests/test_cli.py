"""Smoke tests da CLI (run/status/resume/list)."""
import os

from click.testing import CliRunner

from orchestrator.cli import cli


def test_cli_run_status_list(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")

    res = cr.invoke(cli, ["run", "--batch", "6", "--run-id", "cli-1", "--db", db, "--config-dir", "config"])
    assert res.exit_code == 0, res.output
    assert "produzidos : 6" in res.output

    res2 = cr.invoke(cli, ["status", "cli-1", "--db", db, "--config-dir", "config"])
    assert res2.exit_code == 0, res2.output
    assert "cli-1" in res2.output
    assert "produzidos : 6" in res2.output

    res3 = cr.invoke(cli, ["list", "--db", db])
    assert res3.exit_code == 0
    assert "cli-1" in res3.output


def test_cli_status_unknown_run_fails(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    # cria o arquivo com um run qualquer primeiro
    cr.invoke(cli, ["run", "--batch", "2", "--run-id", "exists", "--db", db, "--config-dir", "config"])
    res = cr.invoke(cli, ["status", "nope", "--db", db, "--config-dir", "config"])
    assert res.exit_code != 0
    assert "não encontrado" in res.output


def test_cli_resume_smoke(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    cr.invoke(cli, ["run", "--batch", "4", "--run-id", "r1", "--db", db, "--config-dir", "config"])
    res = cr.invoke(cli, ["resume", "r1", "--db", db, "--config-dir", "config"])
    assert res.exit_code == 0, res.output
    assert "run r1" in res.output


def test_cli_loop_runs_n_cycles(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    store = str(tmp_path / "feedback.json")
    res = cr.invoke(cli, [
        "loop", "--cycles", "2", "--batch", "6", "--run-id-prefix", "L",
        "--db", db, "--feedback-store", store, "--config-dir", "config",
    ])
    assert res.exit_code == 0, res.output
    assert "ciclo 1/2" in res.output
    assert "ciclo 2/2" in res.output
    # ambos os ciclos foram persistidos no store e checkpointados
    res2 = cr.invoke(cli, ["list", "--db", db])
    assert "L-c1" in res2.output
    assert "L-c2" in res2.output


def test_cli_loop_requires_feedback_store(tmp_path):
    cr = CliRunner()
    db = str(tmp_path / "runs.sqlite")
    res = cr.invoke(cli, [
        "loop", "--cycles", "2", "--db", db, "--config-dir", "config",
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
