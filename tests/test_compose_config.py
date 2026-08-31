"""Validação da configuração efetiva do Docker Compose e separação de papéis."""
from __future__ import annotations

import subprocess
from urllib.parse import urlparse

import yaml


def _get_compose_config() -> dict:
    result = subprocess.run(
        ["docker", "compose", "config"],
        capture_output=True,
        text=True,
        check=True,
    )
    return yaml.safe_load(result.stdout)


def test_compose_role_separation_and_dependencies():
    config = _get_compose_config()
    services = config.get("services", {})

    assert "postgres" in services, "Serviço postgres deve existir"
    assert "db-roles" in services, "Serviço one-shot db-roles deve existir"
    assert "migrate" in services, "Serviço migrate deve existir"
    assert "api" in services, "Serviço api deve existir"

    # 1. Dependências do db-roles
    db_roles = services["db-roles"]
    assert db_roles.get("restart") == "no", "db-roles deve ser one-shot (restart: 'no')"
    db_roles_deps = db_roles.get("depends_on", {})
    assert "postgres" in db_roles_deps, "db-roles deve depender de postgres"
    postgres_dep_condition = (
        db_roles_deps["postgres"].get("condition")
        if isinstance(db_roles_deps["postgres"], dict)
        else db_roles_deps["postgres"]
    )
    assert postgres_dep_condition == "service_healthy", "db-roles deve esperar postgres healthy"

    # 2. Dependências do migrate
    migrate = services["migrate"]
    assert migrate.get("restart") == "no", "migrate deve ser one-shot (restart: 'no')"
    migrate_deps = migrate.get("depends_on", {})
    assert "postgres" in migrate_deps, "migrate deve depender de postgres"
    assert "db-roles" in migrate_deps, "migrate deve depender de db-roles"
    db_roles_dep_condition = (
        migrate_deps["db-roles"].get("condition")
        if isinstance(migrate_deps["db-roles"], dict)
        else migrate_deps["db-roles"]
    )
    assert db_roles_dep_condition == "service_completed_successfully", (
        "migrate deve esperar db-roles concluir com sucesso"
    )

    # 3. Dependências da API
    api = services["api"]
    api_deps = api.get("depends_on", {})
    assert "migrate" in api_deps, "api deve depender de migrate"
    migrate_dep_condition = (
        api_deps["migrate"].get("condition")
        if isinstance(api_deps["migrate"], dict)
        else api_deps["migrate"]
    )
    assert migrate_dep_condition == "service_completed_successfully", (
        "api deve esperar migrate concluir com sucesso"
    )

    # 4. URLs de banco no migrate e na api
    migrate_env = migrate.get("environment", {})
    api_env = api.get("environment", {})

    mig_url = migrate_env.get("MIGRATION_DATABASE_URL")
    app_url = api_env.get("DATABASE_URL")

    assert mig_url, "MIGRATION_DATABASE_URL deve estar configurada"
    assert app_url, "DATABASE_URL deve estar configurada"

    mig_parsed = urlparse(mig_url)
    app_parsed = urlparse(app_url)

    assert mig_parsed.username == "orchestrator", (
        f"MIGRATION_DATABASE_URL deve usar papel 'orchestrator', obteve {mig_parsed.username}"
    )
    assert app_parsed.username == "orchestrator_runtime", (
        f"DATABASE_URL deve usar papel 'orchestrator_runtime', obteve {app_parsed.username}"
    )
    assert mig_url != app_url, "DATABASE_URL e MIGRATION_DATABASE_URL nunca podem ser iguais"
