"""Guardas de camadas (layering) e costuras de inversão de adapters.

Trava as fronteiras acordadas entre pacotes:

- ``orchestrator.db`` não conhece ``orchestrator.storage.db`` nem o grafo
  (registros canônicos vivem em módulo neutro; migrações não sobem checkpointer).
- ``CompositeAdapter`` expõe accessors explícitos (``is_mock``, ``get_role``,
  ``has_role``) e a detecção de adapter pago deixa de farejar nomes de classe.
- Constantes do provider ElevenLabs chegam às tools via registry, nunca pelo
  módulo concreto do adapter.
- O fallback mock do vídeo Replicate é injetado pela composition root; o módulo
  pago não importa MockAdapter.
- A submissão terminal do mock é símbolo público.

Os guardas de importação leem o fonte dos módulos (AST/texto) — barato e sem
rede — no mesmo estilo dos guardas de contrato de infra existentes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import orchestrator
from orchestrator.tools.base import (
    ToolContext,
    direct_creator_image_enabled,
    is_paid_creator_adapter,
)

_ROOT = Path(orchestrator.__file__).resolve().parent


def _module_source(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


# --- 1. Ciclo db <-> storage de artifacts -------------------------------------


async def test_artifact_record_has_neutral_home_shared_by_both_sides(tmp_path):
    from orchestrator.storage import records
    from orchestrator.storage.db import ArtifactDB, ArtifactRecord

    assert hasattr(records, "ArtifactRecord")
    assert ArtifactRecord is records.ArtifactRecord

    artifact = records.ArtifactRecord(
        run_id="run-layering",
        kind="clip",
        storage_backend="local",
        storage_key="runs/run-layering/clip.mp4",
        content_type="video/mp4",
        size_bytes=128,
        sha256="abc123",
        meta={"tier": "ltx"},
    )
    repository = ArtifactDB(tmp_path / "artifacts.sqlite")
    repository.setup()

    assert await repository.record(artifact) is artifact
    assert await repository.get(artifact.id) == artifact


def test_db_artifacts_module_does_not_import_storage_db():
    source = _module_source("db/artifacts.py")
    imported = _imported_modules(source)
    forbidden = {name for name in imported if name.startswith("orchestrator.storage")} - {
        "orchestrator.storage.records",
        "orchestrator.storage.retention",
    }
    assert not forbidden, f"db/artifacts.py deve usar os módulos neutros; achou {sorted(forbidden)}"


async def test_open_artifact_repository_accepts_injected_postgres_factory(monkeypatch, tmp_path):
    from orchestrator.storage.db import open_artifact_repository

    class _StubDatabase:
        async def resolve_tenant(self, identity):
            return f"tenant({identity})"

    import orchestrator.db as db_pkg

    async def fake_shared_database():
        return _StubDatabase()

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    monkeypatch.setattr(db_pkg, "get_shared_database", fake_shared_database)
    monkeypatch.setattr(
        db_pkg.TenantIdentity,
        "from_env",
        classmethod(lambda cls: "identity-1"),
    )

    sentinel = object()
    seen: list[tuple] = []

    def postgres_factory(database, tenant):
        seen.append((database, tenant))
        return sentinel

    async with open_artifact_repository(
        tmp_path / "artifacts.db",
        postgres_factory=postgres_factory,
    ) as repository:
        assert repository is sentinel
    assert len(seen) == 1


# --- 2. Migrações sem dependência do grafo ------------------------------------


def test_db_migrations_module_does_not_know_graph():
    source = _module_source("db/migrations.py")
    assert "graph" not in source
    assert "checkpoint" not in source.lower()


def test_cli_migrate_wires_postgres_checkpointer_after_upgrade():
    """O comando migrate chama upgrade + setup do checkpointer na camada CLI."""
    import click.testing

    from orchestrator import cli

    calls: list[str] = []

    def fake_upgrade(database_url: str) -> None:
        calls.append(f"upgrade:{database_url}")

    def fake_setup(database_url: str) -> None:
        calls.append(f"checkpointer:{database_url}")

    async def noop_seed() -> None:
        calls.append("seed")

    originals = (
        cli.upgrade_database,
        cli.setup_postgres_checkpointer,
        cli._seed_default_dev_quotas,
    )
    cli.upgrade_database = fake_upgrade
    cli.setup_postgres_checkpointer = fake_setup
    cli._seed_default_dev_quotas = noop_seed
    try:
        runner = click.testing.CliRunner()
        # O `.env` local pode definir MIGRATION_DATABASE_URL (envvar da opção);
        # fixamos ambas para o teste ficar hermético.
        result = runner.invoke(
            cli.cli,
            ["migrate"],
            env={
                "DATABASE_URL": "postgresql://migrate-test",
                "MIGRATION_DATABASE_URL": "",
                "ORCH_ENV": "local",
            },
        )
    finally:
        (
            cli.upgrade_database,
            cli.setup_postgres_checkpointer,
            cli._seed_default_dev_quotas,
        ) = originals

    assert result.exit_code == 0, result.output
    assert calls == [
        "upgrade:postgresql://migrate-test",
        "checkpointer:postgresql://migrate-test",
        "seed",
    ]


# --- 3. Accessors explícitos do CompositeAdapter ------------------------------


def _composite(monkeypatch_registry=None, **roles):
    from orchestrator.registry import CompositeAdapter

    base = {
        "creator": object(),
        "video": object(),
        "qc": object(),
        "assembly": object(),
        "upscale": object(),
    }
    base.update(roles)
    return CompositeAdapter(by_role=base)


def test_mock_adapter_exposes_is_mock(pipeline_cfg):
    from orchestrator.adapters.mock import MockAdapter

    assert MockAdapter(tiers=pipeline_cfg["tiers"]).is_mock is True


class _PaidCreator:
    """Adapter creator pago genérico — nome deliberadamente não-mock."""

    is_mock = False


def test_composite_is_mock_true_only_when_every_role_is_mock(pipeline_cfg):
    from orchestrator.adapters.mock import MockAdapter

    mock = MockAdapter(tiers=pipeline_cfg["tiers"])
    assert _composite(creator=mock, video=mock, qc=mock, assembly=mock, upscale=mock).is_mock
    assert not _composite(creator=_PaidCreator()).is_mock


def test_composite_role_accessors_return_wired_adapters(pipeline_cfg):
    composite = _composite(creator="CREATOR", video="VIDEO")
    assert composite.has_role("creator")
    assert composite.get_role("creator") == "CREATOR"
    assert composite.get_role("video") == "VIDEO"
    assert not composite.has_role("judge")
    assert composite.get_role("judge") is None


def _ctx(adapter) -> ToolContext:
    return ToolContext(adapter=adapter, pipeline={}, run={}, run_id="run-1")


def test_is_mock_flag_short_circuits_paid_detection(pipeline_cfg):
    from orchestrator.adapters.mock import MockAdapter

    assert is_paid_creator_adapter(_ctx(MockAdapter(tiers=pipeline_cfg["tiers"]))) is False
    assert is_paid_creator_adapter(_ctx(None)) is False
    assert is_paid_creator_adapter(_ctx(_PaidCreator())) is True


def test_is_paid_creator_adapter_uses_composite_accessors_not_class_names():
    from orchestrator.adapters.mock import MockAdapter

    paid = _PaidCreator()
    all_mocks = _composite(
        creator=MockAdapter(tiers=[]),
        video=MockAdapter(tiers=[]),
        qc=MockAdapter(tiers=[]),
        assembly=MockAdapter(tiers=[]),
        upscale=MockAdapter(tiers=[]),
    )
    mixed = _composite(creator=paid)

    assert is_paid_creator_adapter(_ctx(all_mocks)) is False
    assert is_paid_creator_adapter(_ctx(mixed)) is True


def test_tools_base_stops_sniffing_adapter_class_names():
    source = _module_source("tools/base.py")
    assert "_by_role" not in source, "tools não deve alcançar estado privado do composite"
    assert '"MockAdapter"' not in source and "'MockAdapter'" not in source, (
        "detecção de mock deve usar accessors, não nome de classe"
    )
    assert "type(adapter).__name__" not in source


def test_direct_creator_image_alias_kept_for_call_sites():
    assert direct_creator_image_enabled is is_paid_creator_adapter


# --- 4. Constantes ElevenLabs via registry ------------------------------------


def test_tools_creators_do_not_import_concrete_voice_adapter():
    imported = _imported_modules(_module_source("tools/creators.py"))
    concrete = {name for name in imported if name.startswith("orchestrator.adapters.")} - {
        "orchestrator.adapters.base"
    }
    assert not concrete, (
        f"tools/creators.py só pode depender de adapters.base; achou {sorted(concrete)}"
    )


def test_registry_exposes_elevenlabs_voice_design_constants():
    from orchestrator.adapters import elevenlabs_voice_design
    from orchestrator.creative_contracts import CreatorVoiceSpec
    from orchestrator.registry import DEFAULT_PREVIEW_TEXT, voice_description_hash

    assert DEFAULT_PREVIEW_TEXT == elevenlabs_voice_design.DEFAULT_PREVIEW_TEXT

    spec = CreatorVoiceSpec(
        language_code="en-US",
        accent="neutral",
        vocal_presentation="feminine",
        vocal_age="young_adult",
        timbre="warm",
        pace="conversational",
        energy="balanced",
        warmth=0.7,
        expressiveness=0.6,
        rationale="layering guard",
    )
    assert voice_description_hash(spec) == elevenlabs_voice_design.voice_description_hash(spec)


# --- 5. Fallback mock do ReplicateVideoAdapter injetado -----------------------


def test_replicate_video_module_does_not_depend_on_mock_adapter():
    imported = _imported_modules(_module_source("adapters/replicate_video.py"))
    assert "orchestrator.adapters.mock" not in imported
    assert "adapters.mock" not in _module_source("adapters/replicate_video.py")


async def test_replicate_video_fallback_uses_injected_generator():
    from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
    from orchestrator.graph.state import Artifact

    tiers = [{"name": "kling", "model": "kling-3.0", "cost_per_second": 0.10}]
    fallback_artifact = Artifact(kind="clip", uri="data:video/mp4;base64,AAA")

    async def injected(*args, **kwargs):
        return fallback_artifact

    adapter = ReplicateVideoAdapter(
        tiers=tiers,
        allow_mock_fallback=True,
        mock_clip_generator=injected,
    )
    artifact = await adapter.generate_clip("item-abc", "kling", 8, 1)
    assert artifact.kind == "clip"
    assert artifact.meta["provider"] == "mock"
    assert artifact.meta["fallback_reason"] == "replicate_model_not_configured"


async def test_replicate_video_without_injected_generator_refuses_fallback():
    from orchestrator.adapters.replicate_video import ReplicateVideoAdapter

    tiers = [{"name": "kling", "model": "kling-3.0", "cost_per_second": 0.10}]
    adapter = ReplicateVideoAdapter(tiers=tiers, allow_mock_fallback=True)
    with pytest.raises(RuntimeError, match="mock fallback disabled"):
        await adapter.generate_clip("item-abc", "kling", 8, 1)


def test_registry_builds_replicate_with_mock_fallback_wired(pipeline_cfg):
    from orchestrator.registry import build_adapter_from_providers

    pipeline = dict(pipeline_cfg)
    pipeline["video"] = {"allow_mock_fallback": True}
    composite = build_adapter_from_providers({"adapters": {"video": "replicate"}}, pipeline)
    generator = composite.get_role("video")
    assert generator.allow_mock_fallback is True
    assert callable(getattr(generator, "_mock_clip_generator", None))


# --- 6. Submissão terminal pública --------------------------------------------


def test_terminal_submission_is_public_and_private_alias_survives():
    import orchestrator.adapters.mock as mock

    assert hasattr(mock, "terminal_submission")
    assert mock._terminal_submission is mock.terminal_submission


def test_language_runtime_uses_public_terminal_submission():
    source = _module_source("language_runtime.py")
    assert "_terminal_submission" not in source
