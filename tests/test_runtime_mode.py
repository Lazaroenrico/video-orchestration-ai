"""Contrato do seletor centralizado de modo de execução (``runtime_mode``).

Cobre: leitura tardia de ``DATABASE_URL``/``ORCH_ENABLE_PAID_ADAPTERS``, o hook de
reset do singleton de throttle Replicate, a seleção de backend dos stores e a
seleção local vs. PostgreSQL das fachadas ``*_store`` (sem servidor PostgreSQL —
a stack é substituída por stubs; integração real fica nos testes ``test_postgres_*``).
"""

from __future__ import annotations

import pytest

from orchestrator import runtime_mode


class _StubDatabase:
    async def resolve_tenant(self, identity):
        return f"tenant({identity})"


def _patch_shared_database(monkeypatch, database: _StubDatabase) -> None:
    import orchestrator.db as db_pkg

    async def fake_shared_database():
        return database

    monkeypatch.setattr(db_pkg, "get_shared_database", fake_shared_database)
    monkeypatch.setattr(
        db_pkg.TenantIdentity,
        "from_env",
        classmethod(lambda cls: "identity-1"),
    )


# --- DATABASE_URL / modo durável ---------------------------------------------


def test_database_url_reads_env_on_each_call(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert runtime_mode.database_url() is None

    monkeypatch.setenv("DATABASE_URL", "postgresql://a")
    first = runtime_mode.database_url()
    assert first == "postgresql://a"

    monkeypatch.setenv("DATABASE_URL", "postgresql://b")
    assert runtime_mode.database_url() == "postgresql://b"
    assert first == "postgresql://a"


def test_is_durable_reflects_env_presence(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert runtime_mode.is_durable() is False

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert runtime_mode.is_durable() is True


# --- ORCH_ENABLE_PAID_ADAPTERS ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("TRUE", True),
        ("Yes", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("on", False),
        ("sim", False),
        ("  true", False),
    ],
)
def test_paid_adapters_enabled_parses_known_values(monkeypatch, raw, expected):
    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", raw)
    assert runtime_mode.paid_adapters_enabled() is expected


def test_paid_adapters_enabled_unset_is_false(monkeypatch):
    monkeypatch.delenv("ORCH_ENABLE_PAID_ADAPTERS", raising=False)
    assert runtime_mode.paid_adapters_enabled() is False


# --- Reset do singleton de throttle Replicate ---------------------------------


def test_reset_replicate_throttle_clears_cached_singleton(monkeypatch):
    from orchestrator.adapters._throttle import (
        get_replicate_throttle,
        reset_replicate_throttle,
    )

    reset_replicate_throttle()
    monkeypatch.delenv("REPLICATE_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("REPLICATE_MAX_CONCURRENCY", raising=False)

    first = get_replicate_throttle()
    assert get_replicate_throttle() is first

    reset_replicate_throttle()

    assert get_replicate_throttle() is not first
    reset_replicate_throttle()


def test_reset_replicate_throttle_makes_env_changes_take_effect(monkeypatch):
    from orchestrator.adapters._throttle import (
        get_replicate_throttle,
        reset_replicate_throttle,
    )

    reset_replicate_throttle()
    monkeypatch.delenv("REPLICATE_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("REPLICATE_MAX_CONCURRENCY", raising=False)

    stale = get_replicate_throttle()
    assert stale.min_interval == 10.0
    assert stale.concurrency == 1

    monkeypatch.setenv("REPLICATE_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("REPLICATE_MAX_CONCURRENCY", "8")

    reset_replicate_throttle()
    fresh = get_replicate_throttle()

    assert fresh is not stale
    assert fresh.min_interval == 0.0
    assert fresh.concurrency == 8
    reset_replicate_throttle()


# --- Seleção de backend dos repositórios --------------------------------------


async def test_open_repository_backend_uses_local_when_not_durable(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    local_calls: list[int] = []
    postgres_calls: list[tuple] = []

    def local_factory():
        local_calls.append(1)
        return "local"

    def postgres_factory(*args):
        postgres_calls.append(args)
        return "postgres"

    async with runtime_mode.open_repository_backend(local_factory, postgres_factory) as repository:
        assert repository == "local"

    assert local_calls == [1]
    assert postgres_calls == []


async def test_open_repository_backend_wires_tenant_scoped_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    stub_database = _StubDatabase()
    _patch_shared_database(monkeypatch, stub_database)

    seen: list[tuple] = []

    def postgres_factory(database, tenant):
        seen.append((database, tenant))
        return ("postgres", database, tenant)

    async with runtime_mode.open_repository_backend(
        pytest.fail,  # backend local não pode ser construído no modo durável
        postgres_factory,
    ) as repository:
        assert repository == ("postgres", stub_database, "tenant(identity-1)")

    assert seen == [(stub_database, "tenant(identity-1)")]


# --- Fachadas de store continuam selecionando sqlite/json vs. PostgreSQL ------


async def test_run_and_job_stores_yield_none_without_database_url(monkeypatch):
    from orchestrator import job_store, run_store

    monkeypatch.delenv("DATABASE_URL", raising=False)

    async with run_store.open_repository() as runs:
        assert runs is None
    async with job_store.open_repository() as jobs:
        assert jobs is None


async def test_run_and_job_stores_select_postgres_when_durable(monkeypatch):
    from orchestrator import run_store
    from orchestrator.db import PostgresRunRepository

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    stub_database = _StubDatabase()
    _patch_shared_database(monkeypatch, stub_database)

    async with run_store.open_repository() as runs:
        assert isinstance(runs, PostgresRunRepository)


async def test_local_facades_keep_json_or_sqlite_backends(monkeypatch, tmp_path):
    from orchestrator.creator_store import JsonCreatorRepository
    from orchestrator.creator_store import open_repository as open_creators
    from orchestrator.feedback_store import JsonFeedbackRepository
    from orchestrator.feedback_store import open_repository as open_feedback
    from orchestrator.prompt_store import JsonPromptRepository
    from orchestrator.prompt_store import open_repository as open_prompts
    from orchestrator.storage.db import ArtifactDB, open_artifact_repository

    monkeypatch.delenv("DATABASE_URL", raising=False)

    async with open_creators(tmp_path / "creators.json") as creators:
        assert isinstance(creators, JsonCreatorRepository)
    async with open_prompts(tmp_path / "prompts.json") as prompts:
        assert isinstance(prompts, JsonPromptRepository)
    async with open_feedback(tmp_path / "feedback.json") as feedback:
        assert isinstance(feedback, JsonFeedbackRepository)
    async with open_artifact_repository(tmp_path / "artifacts.db") as artifacts:
        assert isinstance(artifacts, ArtifactDB)


@pytest.mark.parametrize(
    ("module_name", "repo_name"),
    [
        ("creator_store", "PostgresCreatorRepository"),
        ("prompt_store", "PostgresPromptRepository"),
        ("feedback_store", "PostgresFeedbackRepository"),
    ],
)
async def test_json_facades_select_postgres_when_durable(
    monkeypatch, tmp_path, module_name, repo_name
):
    import importlib

    module = importlib.import_module(f"orchestrator.{module_name}")
    postgres_repository = getattr(importlib.import_module("orchestrator.db"), repo_name)

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    stub_database = _StubDatabase()
    _patch_shared_database(monkeypatch, stub_database)

    async with module.open_repository(tmp_path / f"{module_name}.json") as repository:
        assert isinstance(repository, postgres_repository)


async def test_artifact_facade_selects_postgres_when_durable(monkeypatch, tmp_path):
    from orchestrator.db import PostgresArtifactRepository
    from orchestrator.storage.db import open_artifact_repository

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    stub_database = _StubDatabase()
    _patch_shared_database(monkeypatch, stub_database)

    async with open_artifact_repository(tmp_path / "artifacts.db") as artifacts:
        assert isinstance(artifacts, PostgresArtifactRepository)


# --- Reset automático do singleton entre testes (teardown do conftest) --------


def test_throttle_left_warm_is_reset_for_next_test(monkeypatch):
    """Deixa o singleton aquecido de propósito; o teardown do conftest deve
    descartá-lo para o teste seguinte."""
    from orchestrator.adapters._throttle import get_replicate_throttle

    monkeypatch.setenv("REPLICATE_MIN_INTERVAL_SECONDS", "3")
    warm = get_replicate_throttle()
    assert warm.min_interval == 3.0
    assert throttle_module()._GLOBAL is warm


def test_next_test_starts_with_cold_throttle():
    """Nenhum teste começa com throttle cacheado do anterior: mudanças de env
    ``REPLICATE_*`` via monkeypatch valem entre testes."""
    assert throttle_module()._GLOBAL is None
    from orchestrator.adapters._throttle import get_replicate_throttle

    fresh = get_replicate_throttle()
    assert throttle_module()._GLOBAL is fresh


def throttle_module():
    import orchestrator.adapters._throttle as throttle_mod

    return throttle_mod


# --- Fachadas delegam a decisão ao seletor central -----------------------------


async def test_facades_route_through_runtime_mode_selector(monkeypatch, tmp_path):
    """Mesmo com ``DATABASE_URL`` presente, desligar o seletor central força o
    fallback local em todas as fachadas — prova de que não há leitura espalhada
    de ``os.environ`` nelas."""
    import orchestrator.db as db_pkg
    from orchestrator.creator_store import JsonCreatorRepository
    from orchestrator.creator_store import open_repository as open_creators
    from orchestrator.feedback_store import JsonFeedbackRepository
    from orchestrator.feedback_store import open_repository as open_feedback
    from orchestrator.job_store import open_repository as open_jobs
    from orchestrator.prompt_store import JsonPromptRepository
    from orchestrator.prompt_store import open_repository as open_prompts
    from orchestrator.run_store import open_repository as open_runs
    from orchestrator.storage.db import ArtifactDB, open_artifact_repository

    async def forbidden_shared_database():
        raise AssertionError("fachada deveria decidir via runtime_mode")

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    monkeypatch.setattr(db_pkg, "get_shared_database", forbidden_shared_database)
    monkeypatch.setattr(runtime_mode, "is_durable", lambda: False)

    async with open_runs() as runs:
        assert runs is None
    async with open_jobs() as jobs:
        assert jobs is None
    async with open_creators(tmp_path / "creators.json") as creators:
        assert isinstance(creators, JsonCreatorRepository)
    async with open_prompts(tmp_path / "prompts.json") as prompts:
        assert isinstance(prompts, JsonPromptRepository)
    async with open_feedback(tmp_path / "feedback.json") as feedback:
        assert isinstance(feedback, JsonFeedbackRepository)
    async with open_artifact_repository(tmp_path / "artifacts.db") as artifacts:
        assert isinstance(artifacts, ArtifactDB)


async def test_open_checkpointer_reads_database_url_via_runtime_mode(monkeypatch, tmp_path):
    """A escolha SQLite vs. PostgreSQL do checkpointer passa por ``runtime_mode``:
    com a URL presente mas o seletor desligado, cai no SQLite local."""
    import orchestrator.graph.checkpoint as checkpoint_mod
    from orchestrator.graph.checkpoint import AsyncSqliteCompatSaver

    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "user-1")
    monkeypatch.setattr(runtime_mode, "database_url", lambda: None)

    def forbidden_postgres(*args, **kwargs):
        raise AssertionError("checkpointer deveria cair no SQLite")

    monkeypatch.setattr(checkpoint_mod, "open_tenant_postgres_checkpointer", forbidden_postgres)

    async with checkpoint_mod.open_checkpointer(tmp_path / "graph.sqlite") as saver:
        assert isinstance(saver, AsyncSqliteCompatSaver)


# --- Knob centralizado substitui os parses espalhados -------------------------


async def test_execute_paid_effect_gates_via_runtime_module(monkeypatch):
    """O gate de adapters pagos consulta o knob central: desligá-lo bloqueia o
    efeito mesmo com ``ORCH_ENABLE_PAID_ADAPTERS=true`` presente no ambiente."""
    from orchestrator.tools.base import ToolContext, execute_paid_effect

    ctx = ToolContext(
        adapter=None,
        pipeline={},
        run={},
        run_id="run-1",
        effect_ledger=None,
        durable=True,
    )
    operation_called = False

    async def operation():
        nonlocal operation_called
        operation_called = True
        return {"ok": True}

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    monkeypatch.setattr(runtime_mode, "paid_adapters_enabled", lambda: False)

    with pytest.raises(RuntimeError, match="ORCH_ENABLE_PAID_ADAPTERS"):
        await execute_paid_effect(
            ctx, effect_key="k", provider="p", units=1, request={}, operation=operation
        )
    assert not operation_called


async def test_execute_paid_effect_gates_on_centralized_knob(monkeypatch):
    """Sem o knob, efeito pago durável falha; com ele, segue para o ledger."""
    from orchestrator.tools.base import ToolContext, execute_paid_effect

    ctx = ToolContext(
        adapter=None,
        pipeline={},
        run={},
        run_id="run-1",
        effect_ledger=None,
        durable=True,
    )
    operation_called = False

    async def operation():
        nonlocal operation_called
        operation_called = True
        return {"ok": True}

    monkeypatch.delenv("ORCH_ENABLE_PAID_ADAPTERS", raising=False)
    with pytest.raises(RuntimeError, match="ORCH_ENABLE_PAID_ADAPTERS"):
        await execute_paid_effect(
            ctx, effect_key="k", provider="p", units=1, request={}, operation=operation
        )

    monkeypatch.setenv("ORCH_ENABLE_PAID_ADAPTERS", "true")
    # Knob liberado mas sem ledger: erro muda para o requisito do ledger.
    with pytest.raises(RuntimeError, match="EffectLedger"):
        await execute_paid_effect(
            ctx, effect_key="k", provider="p", units=1, request={}, operation=operation
        )
    assert not operation_called
