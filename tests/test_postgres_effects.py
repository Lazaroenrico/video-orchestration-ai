"""Quota e idempotência de efeitos externos pagos."""
from __future__ import annotations

import asyncio

import pytest

from orchestrator.db import (
    Database,
    PostgresEffectLedger,
    QuotaExceededError,
    TenantIdentity,
    UncertainEffectError,
    provision_runtime_role,
    upgrade_database,
)


def _database_url(postgresql, user=None, password=None):
    info = postgresql.info
    credentials = user or info.user
    if password is not None:
        credentials = f"{credentials}:{password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


def _runtime_url(postgresql):
    admin_url = _database_url(postgresql)
    upgrade_database(admin_url)
    provision_runtime_role(admin_url, "runtime-test-secret")
    return _database_url(
        postgresql,
        "orchestrator_runtime",
        "runtime-test-secret",
    )


async def test_effect_reservation_is_idempotent_and_uncertain_is_never_reissued(
    postgresql,
):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("effects-acme", "Effects Acme", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        ledger = PostgresEffectLedger(database, tenant)
        await ledger.set_quota("replicate", limit_units=2)
        first = await ledger.reserve(
            "run-1:video:item-1:take-1",
            run_id="run-1",
            provider="replicate",
            units=2,
            request={"model": "mock/model"},
        )
        duplicate = await ledger.reserve(
            "run-1:video:item-1:take-1",
            run_id="run-1",
            provider="replicate",
            units=2,
            request={"model": "mock/model"},
        )
        uncertain = await ledger.mark_uncertain(
            "run-1:video:item-1:take-1",
            error="provider timed out after submit",
        )
        with pytest.raises(UncertainEffectError, match="incerto"):
            await ledger.reserve(
                "run-1:video:item-1:take-1",
                run_id="run-1",
                provider="replicate",
                units=2,
                request={"model": "mock/model"},
            )
        with pytest.raises(QuotaExceededError, match="quota"):
            await ledger.reserve(
                "run-1:video:item-2:take-1",
                run_id="run-1",
                provider="replicate",
                units=1,
                request={"model": "mock/model"},
            )
        usage = await ledger.quota_usage("replicate")

    assert first.created is True and first.status == "reserved"
    assert duplicate.created is False
    assert uncertain.status == "uncertain"
    assert usage == (2, 2)


async def test_provider_quota_is_global_across_concurrent_runners(postgresql):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("quota-acme", "Quota Acme", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        ledger = PostgresEffectLedger(database, tenant)
        await ledger.set_quota("replicate", limit_units=2)

        async def reserve(effect_key):
            return await ledger.reserve(
                effect_key,
                run_id="run-quota",
                provider="replicate",
                units=2,
                request={"model": "mock/model"},
            )

        results = await asyncio.gather(
            reserve("run-quota:video:item-1"),
            reserve("run-quota:video:item-2"),
            return_exceptions=True,
        )
        usage = await ledger.quota_usage("replicate")

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, QuotaExceededError) for result in results) == 1
    assert usage == (2, 2)


async def test_succeeded_effect_replays_persisted_result_without_reissue(postgresql):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("result-acme", "Result Acme", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        ledger = PostgresEffectLedger(database, tenant)
        await ledger.set_quota("replicate", limit_units=1)
        await ledger.reserve(
            "run-result:video:item-1",
            run_id="run-result",
            provider="replicate",
            units=1,
            request={"model": "mock/model"},
        )
        succeeded = await ledger.mark_succeeded(
            "run-result:video:item-1",
            result={"provider_id": "prediction-1"},
        )
        replay = await ledger.reserve(
            "run-result:video:item-1",
            run_id="run-result",
            provider="replicate",
            units=1,
            request={"model": "mock/model"},
        )

    assert succeeded.status == "succeeded"
    assert replay.created is False
    assert replay.result == {"provider_id": "prediction-1"}


async def test_effect_ledger_rejects_invalid_and_ambiguous_transitions(postgresql):
    runtime_url = _runtime_url(postgresql)
    identity = TenantIdentity("guard-acme", "Guard Acme", "oidc|runner")

    async with Database(runtime_url) as database:
        tenant = await database.ensure_tenant(identity)
        ledger = PostgresEffectLedger(database, tenant)
        with pytest.raises(ValueError, match="negativa"):
            await ledger.set_quota("replicate", limit_units=-1)
        with pytest.raises(QuotaExceededError, match="não configurada"):
            await ledger.reserve(
                "missing-quota",
                run_id="run-guard",
                provider="replicate",
                units=1,
                request={},
            )
        await ledger.set_quota("replicate", limit_units=1)
        with pytest.raises(ValueError, match="positivo"):
            await ledger.reserve(
                "zero-units",
                run_id="run-guard",
                provider="replicate",
                units=0,
                request={},
            )
        await ledger.reserve(
            "effect-guard",
            run_id="run-guard",
            provider="replicate",
            units=1,
            request={"model": "one"},
        )
        with pytest.raises(ValueError, match="payload diferente"):
            await ledger.reserve(
                "effect-guard",
                run_id="run-guard",
                provider="replicate",
                units=1,
                request={"model": "two"},
            )
        with pytest.raises(QuotaExceededError, match="consumo atual"):
            await ledger.set_quota("replicate", limit_units=0)
        with pytest.raises(ValueError, match="inexistente"):
            await ledger.mark_uncertain("missing-effect", error="missing")
        with pytest.raises(ValueError, match="inexistente"):
            await ledger.mark_succeeded("missing-effect", result={})
        await ledger.mark_uncertain("effect-guard", error="timeout")
        with pytest.raises(UncertainEffectError, match="não pode"):
            await ledger.mark_succeeded("effect-guard", result={})
        with pytest.raises(ValueError, match="inexistente"):
            await ledger.quota_usage("missing-provider")

        # Concluir duas vezes é idempotente e preserva o primeiro resultado.
        await ledger.set_quota("other", limit_units=1)
        await ledger.reserve(
            "effect-succeeded",
            run_id="run-guard",
            provider="other",
            units=1,
            request={},
        )
        first = await ledger.mark_succeeded(
            "effect-succeeded",
            result={"provider_id": "first"},
        )
        replay = await ledger.mark_succeeded(
            "effect-succeeded",
            result={"provider_id": "second"},
        )

    assert first.result == replay.result == {"provider_id": "first"}
