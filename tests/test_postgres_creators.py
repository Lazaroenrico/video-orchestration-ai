"""Integração real do repositório PostgreSQL de creators (ADR-D36, Fase 2)."""
from __future__ import annotations

from uuid import UUID

from fastapi import BackgroundTasks

from orchestrator.db import (
    Database,
    PostgresCreatorRepository,
    PostgresJobRepository,
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
            CREATE ROLE creator_app LOGIN PASSWORD 'creator_app';
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE creator_app LOGIN PASSWORD 'creator_app';
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO creator_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO creator_app"
    )
    postgresql.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO creator_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://creator_app:creator_app@{info.host}:{info.port}/{info.dbname}"


async def test_recorded_creator_survives_repository_restart(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    creator = {
        "id": "creator-0",
        "image_uri": "r2://ugc-prod/run-1/creator-0/image.webp",
        "voice_ref": "voice-acme",
        "voice_preview_uri": "r2://ugc-prod/run-1/creator-0/voice.mp3",
        "angles": ["front", "profile"],
        "voice_reroll_count": 2,
        "voice_spec": {
            "language_code": "pt-BR",
            "timbre": "warm",
        },
        "voice_provider": "elevenlabs",
        "voice_tts_model": "eleven_turbo_v2_5",
        "voice_design_batch": {
            "provider": "elevenlabs",
            "design_model": "eleven_ttv_v3",
            "description_hash": "description-hash",
            "prompt_version": "voice-match-v1",
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "preview": {
                        "kind": "voice_preview",
                        "uri": (
                            "r2://ugc-prod/run-1/creators/creator-0/"
                            "voice-candidates/description-hash/candidate-1.mp3"
                        ),
                    },
                    "duration_seconds": 4.2,
                    "media_type": "audio/mpeg",
                }
            ],
            "cost_usd": 0.01,
            "cost_source": "estimate",
        },
        "selected_voice_candidate_id": "candidate-1",
    }

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        repository = PostgresCreatorRepository(database, tenant)
        await repository.record_creators(
            "run-1",
            [creator],
            approved_ids=["creator-0"],
            creator_prompt="natural",
            video_prompt="close-up",
            offer="serum X",
        )

    async with Database(runtime_url) as restarted_database:
        tenant = await restarted_database.ensure_tenant(identity)
        creators = await PostgresCreatorRepository(
            restarted_database, tenant
        ).load_creators()

    assert creators == [
        {
            "run_id": "run-1",
            "creator_id": "creator-0",
            "image_uri": "r2://ugc-prod/run-1/creator-0/image.webp",
            "voice_ref": "voice-acme",
            "voice_preview_uri": "r2://ugc-prod/run-1/creator-0/voice.mp3",
            "image": "r2://ugc-prod/run-1/creator-0/image.webp",
            "voice": "voice-acme",
            "angles": ["front", "profile"],
            "voice_reroll_count": 2,
            "voice_spec": {
                "language_code": "pt-BR",
                "timbre": "warm",
            },
            "voice_provider": "elevenlabs",
            "voice_design_model": "eleven_ttv_v3",
            "voice_tts_model": "eleven_turbo_v2_5",
            "voice_design_hash": "description-hash",
            "voice_selected_candidate": "candidate-1",
            "voice_status": "selected",
            "voice_design_meta": {
                "prompt_version": "voice-match-v1",
                "reroll": 2,
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "preview_uri": (
                            "r2://ugc-prod/run-1/creators/creator-0/"
                            "voice-candidates/description-hash/candidate-1.mp3"
                        ),
                        "duration_seconds": 4.2,
                        "media_type": "audio/mpeg",
                    }
                ],
                "cost_usd": 0.01,
                "cost_source": "estimate",
            },
            "creator_prompt": "natural",
            "video_prompt": "close-up",
            "offer": "serum X",
            "status": "approved",
        }
    ]


async def test_recording_same_run_creator_updates_without_duplication(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresCreatorRepository(database, tenant)
        await repository.record_creators(
            "run-1",
            [{"id": "creator-0", "voice_ref": "voice-original"}],
            approved_ids=["creator-0"],
        )
        await repository.record_creators(
            "run-2",
            [{"id": "creator-1", "voice_ref": "voice-other"}],
            approved_ids=[],
        )
        await repository.record_creators(
            "run-1",
            [{"id": "creator-0", "voice_ref": "voice-rerolled"}],
            approved_ids=[],
        )

        creators = await repository.load_creators()

    assert [(item["run_id"], item["creator_id"]) for item in creators] == [
        ("run-1", "creator-0"),
        ("run-2", "creator-1"),
    ]
    assert creators[0]["voice_ref"] == "voice-rerolled"
    assert creators[0]["status"] == "rejected"


async def test_creator_lookup_selects_requested_run_or_newest_version(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        repository = PostgresCreatorRepository(database, tenant)
        await repository.record_creators(
            "run-old",
            [{"id": "creator-0", "voice_ref": "voice-old"}],
            approved_ids=["creator-0"],
        )
        await repository.record_creators(
            "run-new",
            [{"id": "creator-0", "voice_ref": "voice-new"}],
            approved_ids=["creator-0"],
        )

        exact = await repository.find_creator("creator-0", "run-old")
        newest = await repository.find_creator("creator-0")
        missing = await repository.find_creator("missing")

    assert exact is not None and exact["voice_ref"] == "voice-old"
    assert newest is not None and newest["voice_ref"] == "voice-new"
    assert missing is None


async def test_creators_api_signs_r2_pointers_without_persisting_signed_urls(
    monkeypatch,
    postgresql,
    tmp_path,
):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    trap = tmp_path / "must-not-be-used.json"
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")
    monkeypatch.setenv("ORCH_CREATORS", str(trap))

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        await PostgresCreatorRepository(database, tenant).record_creators(
            "run-1",
            [{
                "id": "creator-0",
                "image_uri": "r2://ugc-prod/run-1/creator-0/image.webp",
                "voice_ref": "voice-acme",
                "voice_preview_uri": "r2://ugc-prod/run-1/creator-0/voice.mp3",
            }],
            approved_ids=["creator-0"],
        )

    class SigningStorage:
        async def get_signed_url(self, key: str, *, ttl_seconds: int) -> str:
            return f"https://signed.example/{key}?ttl={ttl_seconds}"

    monkeypatch.setattr(web_server, "_signing_storage", lambda _config: SigningStorage())

    payload = await web_server.creators_history()

    assert payload["store_path"] == "postgresql"
    assert payload["exists"] is True
    assert payload["creators"][0]["image_uri"] == (
        "https://signed.example/run-1/creator-0/image.webp?ttl=900"
    )
    assert payload["creators"][0]["voice_preview_uri"] == (
        "https://signed.example/run-1/creator-0/voice.mp3?ttl=900"
    )
    assert not trap.exists()

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        persisted = await PostgresCreatorRepository(database, tenant).load_creators()

    assert persisted[0]["image_uri"] == "r2://ugc-prod/run-1/creator-0/image.webp"
    assert persisted[0]["voice_preview_uri"] == "r2://ugc-prod/run-1/creator-0/voice.mp3"


async def test_reused_postgres_creator_stays_canonical_in_durable_handoff(
    monkeypatch,
    postgresql,
):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    monkeypatch.setenv("DATABASE_URL", runtime_url)
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|alice")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        await PostgresCreatorRepository(database, tenant).record_creators(
            "run-source",
            [{
                "id": "creator-0",
                "image_uri": "r2://ugc-prod/run-source/creator-0/image.webp",
                "voice_ref": "voice-acme",
                "voice_preview_uri": "r2://ugc-prod/run-source/creator-0/voice.mp3",
            }],
            approved_ids=["creator-0"],
        )

    background = BackgroundTasks()

    response = await web_server.start_run(
        web_server.RunRequest(
            creator_id="creator-0",
            creator_run_id="run-source",
            approve_creators=False,
            edit_concepts=False,
        ),
        background,
    )

    assert background.tasks == []
    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        job = await PostgresJobRepository(database, tenant).get(
            UUID(response["job_id"])
        )

    assert job is not None
    seed_creator = job.payload["seed_creator"]
    assert seed_creator["image_uri"] == (
        "r2://ugc-prod/run-source/creator-0/image.webp"
    )
    assert seed_creator["voice_preview_uri"] == (
        "r2://ugc-prod/run-source/creator-0/voice.mp3"
    )

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        persisted = await PostgresCreatorRepository(database, tenant).find_creator(
            "creator-0", "run-source"
        )

    assert persisted is not None
    assert persisted["image_uri"] == "r2://ugc-prod/run-source/creator-0/image.webp"


async def test_creators_are_isolated_between_organizations(postgresql):
    upgrade_database(_admin_url(postgresql))
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        acme = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        globex = await database.ensure_tenant(
            TenantIdentity("globex", "Globex", "oidc|bob")
        )
        acme_repository = PostgresCreatorRepository(database, acme)
        globex_repository = PostgresCreatorRepository(database, globex)
        await acme_repository.record_creators(
            "run-acme",
            [{"id": "creator-0", "voice_ref": "acme-secret"}],
            approved_ids=["creator-0"],
        )
        await globex_repository.record_creators(
            "run-globex",
            [{"id": "creator-0", "voice_ref": "globex-private"}],
            approved_ids=["creator-0"],
        )

        assert await globex_repository.find_creator("creator-0", "run-acme") is None

        async with database.connection(globex) as connection:
            cursor = await connection.execute(
                "SELECT run_id, voice_ref FROM creators ORDER BY run_id"
            )
            raw_visible_rows = await cursor.fetchall()
            cross_tenant_update = await connection.execute(
                "UPDATE creators SET voice_ref = 'compromised' WHERE run_id = 'run-acme'"
            )

        acme_creator = await acme_repository.find_creator("creator-0", "run-acme")

    assert raw_visible_rows == [("run-globex", "globex-private")]
    assert cross_tenant_update.rowcount == 0
    assert acme_creator is not None and acme_creator["voice_ref"] == "acme-secret"


async def test_migration_upgrades_existing_prompts_from_revision_0002(postgresql):
    admin_url = _admin_url(postgresql)
    upgrade_database(admin_url, "20260718_0002")
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("existing", "Existing", "oidc|existing")

    async with Database(runtime_url) as database:
        before_upgrade = await database.ensure_tenant(identity)
        prompt_repository = PostgresPromptRepository(database, before_upgrade)
        saved_prompt = await prompt_repository.save_template(
            kind="creator",
            title="Existing prompt",
            text="preserve me",
        )

    upgrade_database(admin_url)
    runtime_url = _runtime_url(postgresql)

    async with Database(runtime_url) as database:
        after_upgrade = await database.ensure_tenant(identity)
        prompts = await PostgresPromptRepository(database, after_upgrade).list_templates()
        creator_repository = PostgresCreatorRepository(database, after_upgrade)
        await creator_repository.record_creators(
            "run-after-upgrade",
            [{"id": "creator-0", "voice_ref": "voice-existing"}],
            approved_ids=["creator-0"],
        )
        creators = await creator_repository.load_creators()

    assert after_upgrade == before_upgrade
    assert prompts == [saved_prompt]
    assert creators[0]["run_id"] == "run-after-upgrade"
