import os

from click.testing import CliRunner

from orchestrator.cli import cli

CLI_OFFLINE_ENV = {
    "LANGSMITH_TRACING": "false",
    "DATABASE_URL": "",
    "MIGRATION_DATABASE_URL": "",
    "ORCHESTRATOR_RUNTIME_PASSWORD": "",
}


def _invoke(cr_or_args, args: list[str] | None = None):
    runner = cr_or_args if args is not None else CliRunner()
    command_args = args if args is not None else cr_or_args
    return runner.invoke(cli, command_args, env=CLI_OFFLINE_ENV)


def test_serve_command_invokes_uvicorn(monkeypatch):
    import uvicorn

    calls: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.setdefault("kwargs", k))
    result = CliRunner().invoke(cli, ["serve", "--port", "9123"], env=CLI_OFFLINE_ENV)

    assert result.exit_code == 0, result.output
    assert calls["kwargs"]["port"] == 9123
    assert calls["kwargs"]["host"] == "0.0.0.0"


def test_api_command_invokes_uvicorn(monkeypatch):
    import uvicorn

    calls: dict = {}

    def fake_run(*args, **kwargs):
        calls["import_string"] = args[0] if args else None
        calls["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = CliRunner().invoke(cli, ["api", "--port", "9200"], env=CLI_OFFLINE_ENV)

    assert result.exit_code == 0, result.output
    assert calls["import_string"] == "orchestrator.web.server:app"
    assert calls["kwargs"]["port"] == 9200
    assert calls["kwargs"]["host"] == "0.0.0.0"


def test_cli_migrate_materializes_state_and_is_idempotent(tmp_path):
    db = tmp_path / "runs.sqlite"
    artifacts = tmp_path / "artifacts.sqlite"
    env = {
        **CLI_OFFLINE_ENV,
        "ORCH_MEDIA": str(tmp_path / "media"),
        "ORCH_VIDEOS": str(tmp_path / "videos"),
    }
    runner = CliRunner()
    first = runner.invoke(
        cli, ["migrate", "--db", str(db), "--artifacts-db", str(artifacts)], env=env
    )
    second = runner.invoke(
        cli, ["migrate", "--db", str(db), "--artifacts-db", str(artifacts)], env=env
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert db.exists() and artifacts.exists()
    assert (tmp_path / "media").is_dir() and (tmp_path / "videos").is_dir()
    assert "estado materializado" in first.output


def test_cli_migrate_refuses_runtime_url_outside_local():
    result = CliRunner().invoke(
        cli,
        ["migrate"],
        env={
            **CLI_OFFLINE_ENV,
            "ORCH_ENV": "production",
            "MIGRATION_DATABASE_URL": "",
            "DATABASE_URL": "postgresql://runtime@database/orchestrator",
        },
    )
    assert result.exit_code != 0
    assert "MIGRATION_DATABASE_URL" in result.output


def test_cli_provision_runtime_requires_password(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_RUNTIME_PASSWORD", raising=False)
    result = CliRunner().invoke(
        cli,
        ["db", "provision-runtime", "--migration-database-url", "postgresql://unused"],
        env=CLI_OFFLINE_ENV,
    )
    assert result.exit_code != 0
    assert "ORCHESTRATOR_RUNTIME_PASSWORD" in result.output


def test_cli_voice_quota_command_exposes_only_operational_buckets():
    result = _invoke(["db", "set-voice-quota", "--help"])
    assert result.exit_code == 0, result.output
    assert "elevenlabs_voice_design_chars" in result.output
    assert "elevenlabs_voice_slots" in result.output
    assert "elevenlabs_tts_chars" in result.output


def test_cli_generic_provider_quota_command_accepts_replicate_video_seconds():
    result = _invoke(["db", "set-provider-quota", "--help"])
    assert result.exit_code == 0, result.output
    assert "--provider" in result.output
    assert "--limit-units" in result.output


def test_campaign_commands_are_not_public():
    result = _invoke(["--help"])
    assert result.exit_code == 0, result.output
    for command in ("run", "loop", "status", "resume", "list"):
        assert f"\n  {command} " not in result.output
    for command in ("api", "serve", "runner", "migrate", "db", "ops", "storage"):
        assert command in result.output


def test_runner_requires_once_and_rejects_campaign_flags():
    missing_once = _invoke(["runner"])
    assert missing_once.exit_code != 0
    assert "runner exige --once" in missing_once.output

    campaign_flag = _invoke(["runner", "--batch", "2"])
    assert campaign_flag.exit_code != 0
    assert "No such option" in campaign_flag.output


def test_runner_once_consumes_one_durable_job(monkeypatch):
    observed: dict[str, str] = {}

    async def fake_run_worker_once(*, worker_id: str) -> bool:
        observed["worker_id"] = worker_id
        return True

    monkeypatch.setattr("orchestrator.cli.run_worker_once", fake_run_worker_once)
    result = _invoke(["runner", "--once", "--worker-id", "runner-cli"])

    assert result.exit_code == 0, result.output
    assert observed == {"worker_id": "runner-cli"}
    assert "job processado" in result.output


def test_cli_loads_dotenv_for_operational_runner(monkeypatch):
    observed: dict[str, str | None] = {}

    async def fake_run_worker_once(*, worker_id: str) -> bool:
        del worker_id
        observed["gateway"] = os.environ.get("AI_GATEWAY_API_KEY")
        return False

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr("orchestrator.cli.run_worker_once", fake_run_worker_once)
    with CliRunner().isolated_filesystem():
        with open(".env", "w", encoding="utf-8") as env_file:
            env_file.write("AI_GATEWAY_API_KEY=from-dotenv\n")
        result = CliRunner().invoke(cli, ["runner", "--once"], env=CLI_OFFLINE_ENV)

    assert result.exit_code == 0, result.output
    assert observed["gateway"] == "from-dotenv"


def test_cli_does_not_override_existing_env_with_dotenv(monkeypatch):
    observed: dict[str, str | None] = {}

    async def fake_run_worker_once(*, worker_id: str) -> bool:
        del worker_id
        observed["gateway"] = os.environ.get("AI_GATEWAY_API_KEY")
        return False

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "already-exported")
    monkeypatch.setattr("orchestrator.cli.run_worker_once", fake_run_worker_once)
    with CliRunner().isolated_filesystem():
        with open(".env", "w", encoding="utf-8") as env_file:
            env_file.write("AI_GATEWAY_API_KEY=from-dotenv\n")
        result = CliRunner().invoke(cli, ["runner", "--once"], env=CLI_OFFLINE_ENV)

    assert result.exit_code == 0, result.output
    assert observed["gateway"] == "already-exported"


def test_operational_migrate_is_idempotent(tmp_path):
    db = tmp_path / "runs.sqlite"
    artifacts = tmp_path / "artifacts.sqlite"
    env = {**CLI_OFFLINE_ENV, "ORCH_MEDIA": str(tmp_path / "media"), "ORCH_VIDEOS": str(tmp_path / "videos")}
    runner = CliRunner()

    first = runner.invoke(cli, ["migrate", "--db", str(db), "--artifacts-db", str(artifacts)], env=env)
    second = runner.invoke(cli, ["migrate", "--db", str(db), "--artifacts-db", str(artifacts)], env=env)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert db.exists() and artifacts.exists()


def test_operational_quota_help_exposes_provider_bucket():
    result = _invoke(["db", "set-provider-quota", "--help"])
    assert result.exit_code == 0, result.output
    assert "--provider" in result.output
    assert "--limit-units" in result.output


def test_cli_sets_tenant_scoped_voice_quota(monkeypatch):
    calls = []

    class FakeDatabase:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def resolve_tenant(self, identity):
            calls.append(("tenant", identity))
            return "tenant-context"

    class FakeLedger:
        def __init__(self, database, tenant):
            calls.append(("ledger", database, tenant))

        async def set_quota(self, bucket, *, limit_units):
            calls.append(("quota", bucket, limit_units))

    monkeypatch.setattr("orchestrator.cli.Database.from_env", lambda: FakeDatabase())
    monkeypatch.setattr("orchestrator.cli.PostgresEffectLedger", FakeLedger)
    result = CliRunner().invoke(
        cli,
        ["db", "set-voice-quota", "--bucket", "elevenlabs_voice_slots", "--limit-units", "3"],
        env=CLI_OFFLINE_ENV,
    )

    assert result.exit_code == 0, result.output
    assert calls[-1] == ("quota", "elevenlabs_voice_slots", 3)
    assert "configurada em 3 unidades" in result.output


def test_cli_sets_tenant_scoped_replicate_video_quota(monkeypatch):
    calls = []

    class FakeDatabase:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def resolve_tenant(self, _identity):
            return "tenant-context"

    class FakeLedger:
        def __init__(self, _database, _tenant):
            pass

        async def set_quota(self, provider, *, limit_units):
            calls.append((provider, limit_units))

    monkeypatch.setattr("orchestrator.cli.Database.from_env", lambda: FakeDatabase())
    monkeypatch.setattr("orchestrator.cli.PostgresEffectLedger", FakeLedger)
    result = CliRunner().invoke(
        cli,
        ["db", "set-provider-quota", "--provider", "replicate_video_seconds", "--limit-units", "120"],
        env=CLI_OFFLINE_ENV,
    )

    assert result.exit_code == 0, result.output
    assert calls == [("replicate_video_seconds", 120)]


def test_cli_preserves_operational_ops_and_storage_commands():
    for args in (["ops", "--help"], ["storage", "--help"], ["runner-service", "--help"], ["sqs-runner", "--help"]):
        result = _invoke(list(args))
        assert result.exit_code == 0, (args, result.output)
