"""Integração real do repositório PostgreSQL de prompts (ADR-D36, Fase 2)."""
from __future__ import annotations

import pytest
from fastapi import BackgroundTasks

from orchestrator.db import (
    Database,
    PostgresPromptRepository,
    TenantIdentity,
    upgrade_database,
)
from orchestrator.web import server as web_server


def _admin_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE prompt_app LOGIN;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO prompt_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO prompt_app"
    )
    postgresql.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO prompt_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://prompt_app@{info.host}:{info.port}/{info.dbname}"


async def test_saved_template_survives_repository_restart(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresPromptRepository(database, tenant)
        saved = await repository.save_template(
            kind="creator",
            title=" Meu Vlog ",
            text=' Prompt com "aspas" e linhas. ',
            desc=" descrição ",
        )

    async with Database(runtime_url) as restarted_database:
        tenant = await restarted_database.ensure_tenant(identity)
        restarted_repository = PostgresPromptRepository(restarted_database, tenant)
        loaded = await restarted_repository.list_templates()

    assert saved == {
        "id": saved["id"],
        "kind": "creator",
        "title": "Meu Vlog",
        "desc": "descrição",
        "text": 'Prompt com "aspas" e linhas.',
    }
    assert loaded == [saved]


async def test_delete_template_is_idempotent(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresPromptRepository(database, tenant)
        saved = await repository.save_template(
            kind="video", title="Contrariano", text="Gancho: verdade"
        )

        assert await repository.delete_template(saved["id"]) is True
        assert await repository.delete_template(saved["id"]) is False
        assert await repository.delete_template("not-a-number") is False
        assert await repository.list_templates() == []


async def test_last_used_preserves_previous_non_empty_values(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresPromptRepository(database, tenant)

        assert await repository.get_last_used() == {}
        await repository.record_last_used(
            creator_prompt=" mulher 30 anos ",
            video_prompt=None,
        )
        await repository.record_last_used(
            creator_prompt="   ",
            video_prompt=" gancho: dor ",
        )
        await repository.record_last_used(creator_prompt=None, video_prompt="   ")

        assert await repository.get_last_used() == {
            "creator": "mulher 30 anos",
            "video": "gancho: dor",
        }


async def test_prompt_endpoints_select_postgres_from_runtime_environment(
    monkeypatch,
    postgresql,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_PROMPTS", str(tmp_path / "must-not-be-used.json"))

    saved = await web_server.save_prompt_template(
        web_server.PromptTemplateRequest(
            kind="video",
            title="Contrariano",
            text="Gancho: verdade",
            desc="d",
        )
    )
    payload = await web_server.prompts_index()

    assert saved["ok"] is True
    assert payload["store_path"] == "postgresql"
    assert payload["exists"] is True
    assert payload["templates"] == [saved["template"]]
    assert not (tmp_path / "must-not-be-used.json").exists()


async def test_templates_are_newest_first_and_filterable_by_kind(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresPromptRepository(database, tenant)
        first = await repository.save_template(kind="creator", title="A", text="a")
        second = await repository.save_template(kind="video", title="B", text="b")
        third = await repository.save_template(kind="creator", title="C", text="c")

        all_templates = await repository.list_templates()
        creator_templates = await repository.list_templates(kind="creator")

    assert [item["id"] for item in all_templates] == [
        third["id"],
        second["id"],
        first["id"],
    ]
    assert [item["id"] for item in creator_templates] == [third["id"], first["id"]]


async def test_prompts_are_isolated_between_organizations(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        acme = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        globex = await database.ensure_tenant(
            TenantIdentity("globex", "Globex", "oidc|bob")
        )
        acme_repository = PostgresPromptRepository(database, acme)
        globex_repository = PostgresPromptRepository(database, globex)
        acme_template = await acme_repository.save_template(
            kind="creator", title="Acme only", text="secret"
        )
        await acme_repository.record_last_used(
            creator_prompt="acme creator",
            video_prompt="acme video",
        )

        assert await globex_repository.list_templates() == []
        assert await globex_repository.get_last_used() == {}
        assert await globex_repository.delete_template(acme_template["id"]) is False

        globex_template = await globex_repository.save_template(
            kind="video", title="Globex only", text="private"
        )

        async with database.connection(globex) as connection:
            visible = await connection.execute(
                "SELECT id, title FROM prompt_templates ORDER BY id"
            )
            raw_visible_rows = await visible.fetchall()
            cross_tenant_update = await connection.execute(
                "UPDATE prompt_templates SET text = 'compromised' WHERE id = %s",
                (int(acme_template["id"]),),
            )

        assert await acme_repository.list_templates() == [acme_template]

    assert raw_visible_rows == [(int(globex_template["id"]), "Globex only")]
    assert cross_tenant_update.rowcount == 0


async def test_postgres_templates_keep_legacy_validation_messages(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresPromptRepository(database, tenant)

        with pytest.raises(ValueError, match="kind inválido"):
            await repository.save_template(kind="banner", title="A", text="t")
        with pytest.raises(ValueError, match="title é obrigatório"):
            await repository.save_template(kind="creator", title=" ", text="t")
        with pytest.raises(ValueError, match="text é obrigatório"):
            await repository.save_template(kind="video", title="A", text=" ")


async def test_start_run_records_last_used_in_postgres(monkeypatch, postgresql):
    upgrade_database(_admin_url(postgresql))
    monkeypatch.setenv("DATABASE_URL", _runtime_url(postgresql))
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    request = web_server.RunRequest(
        offer="serum X",
        creator_prompt="mulher 30 anos",
        video_prompt="gancho: erro comum",
        approve_creators=False,
    )

    await web_server.start_run(request, BackgroundTasks())
    payload = await web_server.prompts_index()

    assert payload["last_used"] == {
        "creator": "mulher 30 anos",
        "video": "gancho: erro comum",
    }


async def test_migration_upgrades_an_existing_tenant_from_revision_0001(postgresql):
    admin_url = _admin_url(postgresql)
    upgrade_database(admin_url, "20260718_0001")
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("existing", "Existing", "oidc|existing")

    async with Database(runtime_url) as database:
        before_upgrade = await database.ensure_tenant(identity)

    upgrade_database(admin_url)
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        after_upgrade = await database.ensure_tenant(identity)
        repository = PostgresPromptRepository(database, after_upgrade)
        saved = await repository.save_template(
            kind="creator", title="After migration", text="durable"
        )

    assert after_upgrade == before_upgrade
    assert saved["title"] == "After migration"
