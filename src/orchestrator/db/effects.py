"""Ledger tenant-scoped para quotas e efeitos externos idempotentes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.database import Database
from orchestrator.db.models import EffectLedger as EffectLedgerModel, ProviderQuota
from orchestrator.db.tenancy import TenantContext


@dataclass(frozen=True)
class EffectReservation:
    effect_key: str
    run_id: str
    provider: str
    units: int
    status: str
    request: dict[str, Any]
    created: bool
    result: dict[str, Any] | None = None
    error: str | None = None


class QuotaExceededError(RuntimeError):
    """O efeito excederia a quota configurada para o provider."""


class UncertainEffectError(RuntimeError):
    """Um efeito possivelmente executado exige reconciliação humana."""


def _reservation(row: tuple[Any, ...], *, created: bool) -> EffectReservation:
    return EffectReservation(
        effect_key=row[0],
        run_id=row[1],
        provider=row[2],
        units=row[3],
        status=row[4],
        request=row[5],
        result=row[6],
        error=row[7],
        created=created,
    )


_EFFECT_COLUMNS = (
    EffectLedgerModel.effect_key,
    EffectLedgerModel.run_id,
    EffectLedgerModel.provider,
    EffectLedgerModel.units,
    EffectLedgerModel.status,
    EffectLedgerModel.request,
    EffectLedgerModel.result,
    EffectLedgerModel.error,
)


class PostgresEffectLedger:
    """Reserva custo antes do side effect e impede reemissão ambígua."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def set_quota(self, provider: str, *, limit_units: int) -> None:
        if limit_units < 0:
            raise ValueError("quota não pode ser negativa")
        stmt = (
            pg_insert(ProviderQuota)
            .values(
                organization_id=self._tenant.organization_id,
                provider=provider,
                limit_units=limit_units,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "provider"],
                set_={
                    "limit_units": limit_units,
                },
                where=(ProviderQuota.used_units <= limit_units),
            )
            .returning(ProviderQuota.provider)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            if await cursor.fetchone() is None:
                raise QuotaExceededError(
                    f"quota {limit_units} menor que o consumo atual de {provider}"
                )

    async def reserve(
        self,
        effect_key: str,
        *,
        run_id: str,
        provider: str,
        units: int,
        request: dict[str, Any],
    ) -> EffectReservation:
        if units <= 0:
            raise ValueError("units deve ser positivo")
        stmt_quota = (
            select(ProviderQuota.limit_units, ProviderQuota.used_units)
            .where(
                ProviderQuota.organization_id == self._tenant.organization_id,
                ProviderQuota.provider == provider,
            )
            .with_for_update()
        )
        stmt_existing = (
            select(*_EFFECT_COLUMNS)
            .where(
                EffectLedgerModel.organization_id == self._tenant.organization_id,
                EffectLedgerModel.effect_key == effect_key,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            quota_cursor = await self._database.execute(connection, stmt_quota)
            quota = await quota_cursor.fetchone()
            if quota is None:
                raise QuotaExceededError(
                    f"quota não configurada para provider {provider!r}"
                )

            existing_cursor = await self._database.execute(connection, stmt_existing)
            existing_row = await existing_cursor.fetchone()
            if existing_row is not None:
                existing = _reservation(existing_row, created=False)
                expected = (run_id, provider, units, request)
                actual = (
                    existing.run_id,
                    existing.provider,
                    existing.units,
                    existing.request,
                )
                if actual != expected:
                    raise ValueError(
                        f"effect_key {effect_key!r} reutilizada com payload diferente"
                    )
                if existing.status == "uncertain":
                    raise UncertainEffectError(
                        f"efeito incerto {effect_key!r} exige reconciliação"
                    )
                return existing

            limit_units, used_units = quota
            if used_units + units > limit_units:
                raise QuotaExceededError(
                    f"quota de {provider!r} excedida: "
                    f"{used_units + units}/{limit_units}"
                )
            stmt_insert_effect = (
                pg_insert(EffectLedgerModel)
                .values(
                    organization_id=self._tenant.organization_id,
                    effect_key=effect_key,
                    run_id=run_id,
                    provider=provider,
                    units=units,
                    status="reserved",
                    request=request,
                )
            )
            stmt_update_quota = (
                update(ProviderQuota)
                .where(
                    ProviderQuota.organization_id == self._tenant.organization_id,
                    ProviderQuota.provider == provider,
                )
                .values(used_units=ProviderQuota.used_units + units)
            )
            await self._database.execute(connection, stmt_insert_effect)
            await self._database.execute(connection, stmt_update_quota)

        return EffectReservation(
            effect_key=effect_key,
            run_id=run_id,
            provider=provider,
            units=units,
            status="reserved",
            request=request,
            created=True,
        )

    async def mark_uncertain(
        self,
        effect_key: str,
        *,
        error: str,
    ) -> EffectReservation:
        stmt = (
            update(EffectLedgerModel)
            .where(
                EffectLedgerModel.organization_id == self._tenant.organization_id,
                EffectLedgerModel.effect_key == effect_key,
            )
            .values(
                status="uncertain",
                error=error[:2000],
            )
            .returning(*_EFFECT_COLUMNS)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
            if row is None:
                raise ValueError(f"efeito {effect_key!r} inexistente")
        return _reservation(row, created=False)

    async def mark_succeeded(
        self,
        effect_key: str,
        *,
        result: dict[str, Any],
    ) -> EffectReservation:
        stmt_update = (
            update(EffectLedgerModel)
            .where(
                EffectLedgerModel.organization_id == self._tenant.organization_id,
                EffectLedgerModel.effect_key == effect_key,
                EffectLedgerModel.status == "reserved",
            )
            .values(
                status="succeeded",
                result=result,
                error=None,
            )
            .returning(*_EFFECT_COLUMNS)
        )
        stmt_existing = (
            select(*_EFFECT_COLUMNS)
            .where(
                EffectLedgerModel.organization_id == self._tenant.organization_id,
                EffectLedgerModel.effect_key == effect_key,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt_update)
            row = await cursor.fetchone()
            if row is None:
                existing_cursor = await self._database.execute(connection, stmt_existing)
                row = await existing_cursor.fetchone()
                if row is None:
                    raise ValueError(f"efeito {effect_key!r} inexistente")
                if row[4] != "succeeded":
                    raise UncertainEffectError(
                        f"efeito {effect_key!r} não pode ser concluído "
                        f"a partir de {row[4]!r}"
                    )
        return _reservation(row, created=False)

    async def quota_usage(self, provider: str) -> tuple[int, int]:
        stmt = (
            select(ProviderQuota.used_units, ProviderQuota.limit_units)
            .where(
                ProviderQuota.organization_id == self._tenant.organization_id,
                ProviderQuota.provider == provider,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"quota de {provider!r} inexistente")
        return row

