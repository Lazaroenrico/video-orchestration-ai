"""Contrato do ambiente de desenvolvimento iniciado por ``scripts/dev-local``."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
DOCKERFILE_PATH = ROOT / "Dockerfile"
DEV_LOCAL_PATH = ROOT / "scripts" / "dev-local"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_compose_uses_local_postgres_for_migrations_and_runtime() -> None:
    compose = _compose()
    services = compose["services"]

    assert set(services) == {"postgres", "migrate", "api", "front"}
    assert services["postgres"]["image"].startswith("postgres:16")
    assert services["postgres"]["ports"] == ["127.0.0.1:55432:5432"]

    local_url = "postgresql://orchestrator:orchestrator@postgres:5432/orchestrator"
    for service_name in ("migrate", "api"):
        environment = services[service_name]["environment"]
        assert environment["DATABASE_URL"] == local_url
        assert environment["MIGRATION_DATABASE_URL"] == local_url
        assert all("neon" not in str(value).lower() for value in environment.values())


def test_compose_waits_for_database_and_migrations_before_api() -> None:
    services = _compose()["services"]

    assert "pg_isready" in " ".join(services["postgres"]["healthcheck"]["test"])
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert (
        services["api"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert services["api"]["environment"]["ORCH_WEB_EMBEDDED_RUNNER"] == "true"
    assert services["api"]["environment"]["ORCH_AUTH_MODE"] == "disabled"
    assert services["api"]["environment"]["ORCH_CONFIG_DIR"] == (
        "${ORCH_DEV_CONFIG_DIR:-config}"
    )
    assert services["api"]["environment"]["ORCH_DEV_STORAGE_BACKEND"] == (
        "${ORCH_DEV_STORAGE_BACKEND:-r2}"
    )


def test_compose_exposes_vite_hot_reload_and_persists_only_local_state() -> None:
    compose = _compose()
    front = compose["services"]["front"]

    assert front["ports"] == ["5173:5173"]
    assert "npm run dev" in " ".join(front["command"])
    assert "./front:/app/front" in front["volumes"]
    assert set(compose["volumes"]) == {
        "postgres-data",
        "orchestrator-state",
        "orchestrator-tmp",
        "front-node-modules",
    }
    assert all("r2" not in volume.lower() for volume in compose["volumes"])


def test_runtime_image_contains_alembic_configuration_and_migrations() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "COPY alembic.ini ./alembic.ini" in dockerfile
    assert "COPY migrations/ ./migrations/" in dockerfile


def _write_live_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "AI_GATEWAY_API_KEY=gateway-secret",
                "REPLICATE_API_TOKEN=replicate-secret",
                "ELEVENLABS_API_KEY=elevenlabs-secret",
                "R2_ACCOUNT_ID=account-secret",
                "R2_ACCESS_KEY_ID=access-secret",
                "R2_SECRET_ACCESS_KEY=r2-secret",
                "R2_BUCKET=ugc-dev",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_dev_local_up_uses_compose_v2_with_dev_defaults(tmp_path) -> None:
    env_file = tmp_path / "dev.env"
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_live_env(env_file)
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "storage=%s config=%s args=%s\\n" '
        '"$ORCH_DEV_STORAGE_BACKEND" "$ORCH_DEV_CONFIG_DIR" "$*" > "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_TEST_LOG": str(log_file),
    }
    env.pop("ORCH_DEV_STORAGE_BACKEND", None)
    env.pop("ORCH_DEV_CONFIG_DIR", None)

    result = subprocess.run(
        [str(DEV_LOCAL_PATH), "up"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocation = log_file.read_text(encoding="utf-8")
    assert "storage=r2 config=config" in invocation
    assert "compose" in invocation
    assert "up --build" in invocation
    assert "gateway-secret" not in result.stdout + result.stderr + invocation


def test_dev_local_up_falls_back_to_compose_v1_for_free_staging_smoke(
    tmp_path,
) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text("", encoding="utf-8")
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    docker_compose = fake_bin / "docker-compose"
    docker_compose.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker_compose.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_DEV_CONFIG_DIR": "config-staging",
        "ORCH_DEV_STORAGE_BACKEND": "local",
        "ORCH_TEST_LOG": str(log_file),
    }

    result = subprocess.run(
        [str(DEV_LOCAL_PATH), "up"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocations = log_file.read_text(encoding="utf-8").splitlines()
    assert "down --remove-orphans" in invocations[0]
    assert "up --build" in invocations[1]


def test_dev_local_quotas_configures_all_voice_buckets_inside_api(tmp_path) -> None:
    env_file = tmp_path / "dev.env"
    _write_live_env(env_file)
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_DEV_CONFIG_DIR": "config",
        "ORCH_TEST_LOG": str(log_file),
    }

    result = subprocess.run(
        [
            str(DEV_LOCAL_PATH),
            "quotas",
            "--design-chars",
            "500",
            "--voice-slots",
            "2",
            "--tts-chars",
            "1000",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocations = log_file.read_text(encoding="utf-8").splitlines()
    expected = {
        "elevenlabs_voice_design_chars": "500",
        "elevenlabs_voice_slots": "2",
        "elevenlabs_tts_chars": "1000",
    }
    for bucket, limit in expected.items():
        assert any(
            "exec -T api orchestrator db set-voice-quota "
            f"--bucket {bucket} --limit-units {limit}" in invocation
            for invocation in invocations
        )
    combined_output = result.stdout + result.stderr + "\n".join(invocations)
    assert "gateway-secret" not in combined_output
    assert "replicate-secret" not in combined_output
    assert "elevenlabs-secret" not in combined_output


def test_dev_local_quotas_rejects_incomplete_arguments_before_compose(tmp_path) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text("", encoding="utf-8")
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_DEV_CONFIG_DIR": "config",
        "ORCH_TEST_LOG": str(log_file),
    }

    result = subprocess.run(
        [
            str(DEV_LOCAL_PATH),
            "quotas",
            "--design-chars",
            "500",
            "--voice-slots",
            "2",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--tts-chars N" in result.stderr
    assert not log_file.exists()


@pytest.mark.parametrize("invalid_limit", ["0", "-1", "many"])
def test_dev_local_quotas_rejects_invalid_limits_before_compose(
    tmp_path,
    invalid_limit,
) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text("", encoding="utf-8")
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_DEV_CONFIG_DIR": "config",
        "ORCH_TEST_LOG": str(log_file),
    }

    result = subprocess.run(
        [
            str(DEV_LOCAL_PATH),
            "quotas",
            "--design-chars",
            invalid_limit,
            "--voice-slots",
            "2",
            "--tts-chars",
            "1000",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "inteiro positivo" in result.stderr
    assert not log_file.exists()


def test_dev_local_quotas_refuses_non_live_config_before_compose(tmp_path) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text("", encoding="utf-8")
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_DEV_CONFIG_DIR": "config-staging",
        "ORCH_TEST_LOG": str(log_file),
    }

    result = subprocess.run(
        [
            str(DEV_LOCAL_PATH),
            "quotas",
            "--design-chars",
            "500",
            "--voice-slots",
            "2",
            "--tts-chars",
            "1000",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "ORCH_DEV_CONFIG_DIR=config" in result.stderr
    assert not log_file.exists()


def test_dev_local_quotas_reports_when_api_is_not_running(tmp_path) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text("", encoding="utf-8")
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" >> "$ORCH_TEST_LOG"\n'
        'case "$*" in *"exec -T api true"*) exit 1;; esac\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_DEV_CONFIG_DIR": "config",
        "ORCH_TEST_LOG": str(log_file),
    }

    result = subprocess.run(
        [
            str(DEV_LOCAL_PATH),
            "quotas",
            "--design-chars",
            "500",
            "--voice-slots",
            "2",
            "--tts-chars",
            "1000",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "serviço api não está em execução" in result.stderr
    invocations = log_file.read_text(encoding="utf-8").splitlines()
    assert any("exec -T api true" in invocation for invocation in invocations)
    assert all("set-voice-quota" not in invocation for invocation in invocations)


def test_dev_local_preflight_lists_missing_names_without_printing_secrets(
    tmp_path,
) -> None:
    env_file = tmp_path / "dev.env"
    env_file.write_text(
        "AI_GATEWAY_API_KEY=do-not-print-this\n",
        encoding="utf-8",
    )
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" > "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ORCH_DEV_ENV_FILE": str(env_file),
        "ORCH_TEST_LOG": str(log_file),
    }
    for name in (
        "AI_GATEWAY_API_KEY",
        "REPLICATE_API_TOKEN",
        "REPLICATE_ELEVENLABS_MODEL",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
    ):
        env.pop(name, None)
    env.pop("ORCH_DEV_STORAGE_BACKEND", None)
    env.pop("ORCH_DEV_CONFIG_DIR", None)

    result = subprocess.run(
        [str(DEV_LOCAL_PATH), "up"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "REPLICATE_API_TOKEN" in output
    assert "R2_BUCKET" in output
    assert "do-not-print-this" not in output
    assert not log_file.exists()


def _fake_v2_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    log_file = tmp_path / "compose.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi\n'
        'printf "%s\\n" "$*" > "$ORCH_TEST_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return (
        {
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "ORCH_DEV_ENV_FILE": str(tmp_path / "absent.env"),
            "ORCH_TEST_LOG": str(log_file),
        },
        log_file,
    )


def test_dev_local_down_preserves_named_volumes(tmp_path) -> None:
    env, log_file = _fake_v2_env(tmp_path)

    result = subprocess.run(
        [str(DEV_LOCAL_PATH), "down"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocation = log_file.read_text(encoding="utf-8")
    assert "down --remove-orphans" in invocation
    assert "--volumes" not in invocation


def test_dev_local_reset_requires_confirmation_and_removes_only_compose_volumes(
    tmp_path,
) -> None:
    env, log_file = _fake_v2_env(tmp_path)

    refused = subprocess.run(
        [str(DEV_LOCAL_PATH), "reset"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    accepted = subprocess.run(
        [str(DEV_LOCAL_PATH), "reset", "--yes"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert refused.returncode == 2
    assert accepted.returncode == 0, accepted.stderr
    invocation = log_file.read_text(encoding="utf-8")
    assert "down --volumes --remove-orphans" in invocation
    assert "r2" not in invocation.lower()
