"""Ledger tenant-scoped para quotas e efeitos externos idempotentes."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from orchestrator.common.statuses import (
    TERMINAL_PREDICTION_STATUSES as _PROVIDER_TERMINAL_STATUSES,
)
from orchestrator.db.database import Database
from orchestrator.db.models import EffectLedger as EffectLedgerModel
from orchestrator.db.models import ProviderQuota
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
    error_type: str | None = None
    provider_operation_id: str | None = None
    provider_status: str | None = None


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
        error_type=row[8],
        provider_operation_id=row[9],
        provider_status=row[10],
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
    EffectLedgerModel.error_type,
    EffectLedgerModel.provider_operation_id,
    EffectLedgerModel.provider_status,
)

_PROVIDER_STATUS_ORDER = {"starting": 0, "processing": 1}


def _merge_provider_status(current: str | None, incoming: str) -> str:
    incoming = incoming.strip().lower()
    valid = set(_PROVIDER_STATUS_ORDER) | set(_PROVIDER_TERMINAL_STATUSES)
    if incoming not in valid:
        raise ValueError(f"provider status inválido: {incoming!r}")
    if current is None:
        return incoming
    if current in _PROVIDER_TERMINAL_STATUSES:
        return current
    if incoming in _PROVIDER_TERMINAL_STATUSES:
        return incoming
    return (
        incoming if _PROVIDER_STATUS_ORDER[incoming] >= _PROVIDER_STATUS_ORDER[current] else current
    )


class PostgresEffectLedger:
    """Reserva custo antes do side effect e impede reemissão ambígua."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    @property
    def organization_slug(self) -> str:
        return self._tenant.organization_slug

    async def get(self, effect_key: str) -> EffectReservation:
        stmt = select(*_EFFECT_COLUMNS).where(
            EffectLedgerModel.organization_id == self._tenant.organization_id,
            EffectLedgerModel.effect_key == effect_key,
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"efeito {effect_key!r} inexistente")
        return _reservation(row, created=False)

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
        stmt_existing = select(*_EFFECT_COLUMNS).where(
            EffectLedgerModel.organization_id == self._tenant.organization_id,
            EffectLedgerModel.effect_key == effect_key,
        )
        async with self._database.connection(self._tenant) as connection:
            quota_cursor = await self._database.execute(connection, stmt_quota)
            quota = await quota_cursor.fetchone()
            if quota is None:
                raise QuotaExceededError(f"quota não configurada para provider {provider!r}")

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
                    raise ValueError(f"effect_key {effect_key!r} reutilizada com payload diferente")
                if existing.status == "uncertain":
                    raise UncertainEffectError(f"efeito incerto {effect_key!r} exige reconciliação")
                return existing

            limit_units, used_units = quota
            if used_units + units > limit_units:
                raise QuotaExceededError(
                    f"quota de {provider!r} excedida: {used_units + units}/{limit_units}"
                )
            stmt_insert_effect = pg_insert(EffectLedgerModel).values(
                organization_id=self._tenant.organization_id,
                effect_key=effect_key,
                run_id=run_id,
                provider=provider,
                units=units,
                status="reserved",
                request=request,
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
        error_type: str | None = None,
    ) -> EffectReservation:
        stmt = (
            update(EffectLedgerModel)
            .where(
                EffectLedgerModel.organization_id == self._tenant.organization_id,
                EffectLedgerModel.effect_key == effect_key,
                EffectLedgerModel.status == "reserved",
            )
            .values(
                status="uncertain",
                error=error[:2000],
                **({"error_type": error_type[:200]} if error_type else {}),
            )
            .returning(*_EFFECT_COLUMNS)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
            if row is None:
                existing = await self._database.execute(
                    connection,
                    select(*_EFFECT_COLUMNS).where(
                        EffectLedgerModel.organization_id == self._tenant.organization_id,
                        EffectLedgerModel.effect_key == effect_key,
                    ),
                )
                row = await existing.fetchone()
                if row is None:
                    raise ValueError(f"efeito {effect_key!r} inexistente")
        return _reservation(row, created=False)

    async def bind_provider_operation(
        self,
        effect_key: str,
        *,
        provider_operation_id: str,
        provider_status: str,
    ) -> EffectReservation:
        operation_id = provider_operation_id.strip()
        if not operation_id:
            raise ValueError("provider_operation_id não pode ser vazio")
        try:
            async with self._database.connection(self._tenant) as connection:
                cursor = await self._database.execute(
                    connection,
                    select(*_EFFECT_COLUMNS)
                    .where(
                        EffectLedgerModel.organization_id == self._tenant.organization_id,
                        EffectLedgerModel.effect_key == effect_key,
                    )
                    .with_for_update(),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError(f"efeito {effect_key!r} inexistente")
                current = _reservation(row, created=False)
                if (
                    current.provider_operation_id is not None
                    and current.provider_operation_id != operation_id
                ):
                    raise ValueError(
                        f"efeito {effect_key!r} já vinculado a {current.provider_operation_id!r}"
                    )
                duplicate = await self._database.execute(
                    connection,
                    select(EffectLedgerModel.effect_key).where(
                        EffectLedgerModel.organization_id == self._tenant.organization_id,
                        EffectLedgerModel.provider == current.provider,
                        EffectLedgerModel.provider_operation_id == operation_id,
                        EffectLedgerModel.effect_key != effect_key,
                    ),
                )
                if await duplicate.fetchone() is not None:
                    raise ValueError(
                        f"provider operation {operation_id!r} já vinculada a outro efeito"
                    )
                merged = _merge_provider_status(current.provider_status, provider_status)
                updated = await self._database.execute(
                    connection,
                    update(EffectLedgerModel)
                    .where(
                        EffectLedgerModel.organization_id == self._tenant.organization_id,
                        EffectLedgerModel.effect_key == effect_key,
                    )
                    .values(
                        provider_operation_id=operation_id,
                        provider_status=merged,
                        status=("reserved" if current.status == "uncertain" else current.status),
                    )
                    .returning(*_EFFECT_COLUMNS),
                )
                updated_row = await updated.fetchone()
                assert updated_row is not None
        except IntegrityError as exc:
            raise ValueError(
                f"provider operation {operation_id!r} já vinculada a outro efeito"
            ) from exc
        return _reservation(updated_row, created=False)

    async def update_provider_status(
        self,
        effect_key: str,
        *,
        provider_status: str,
        error_type: str | None = None,
    ) -> EffectReservation:
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(
                connection,
                select(*_EFFECT_COLUMNS)
                .where(
                    EffectLedgerModel.organization_id == self._tenant.organization_id,
                    EffectLedgerModel.effect_key == effect_key,
                )
                .with_for_update(),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError(f"efeito {effect_key!r} inexistente")
            current = _reservation(row, created=False)
            merged = _merge_provider_status(current.provider_status, provider_status)
            values: dict[str, Any] = {"provider_status": merged}
            if error_type:
                values["error_type"] = error_type[:200]
            updated = await self._database.execute(
                connection,
                update(EffectLedgerModel)
                .where(
                    EffectLedgerModel.organization_id == self._tenant.organization_id,
                    EffectLedgerModel.effect_key == effect_key,
                )
                .values(**values)
                .returning(*_EFFECT_COLUMNS),
            )
            updated_row = await updated.fetchone()
            assert updated_row is not None
        return _reservation(updated_row, created=False)

    async def wait_for_provider_operation(
        self,
        effect_key: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 1.0,
    ) -> EffectReservation:
        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            reservation = await self.get(effect_key)
            if reservation.provider_operation_id:
                return reservation
            if time.monotonic() >= deadline:
                return reservation
            await asyncio.sleep(max(poll_interval_seconds, 0))

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
                provider_status="succeeded",
            )
            .returning(*_EFFECT_COLUMNS)
        )
        stmt_existing = select(*_EFFECT_COLUMNS).where(
            EffectLedgerModel.organization_id == self._tenant.organization_id,
            EffectLedgerModel.effect_key == effect_key,
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
                        f"efeito {effect_key!r} não pode ser concluído a partir de {row[4]!r}"
                    )
        return _reservation(row, created=False)

    async def mark_failed(
        self,
        effect_key: str,
        *,
        error: str,
        release_quota: bool,
        error_type: str | None = None,
    ) -> EffectReservation:
        """Mark a definitive failure; release quota at most once when unbilled."""
        stmt_update = (
            update(EffectLedgerModel)
            .where(
                EffectLedgerModel.organization_id == self._tenant.organization_id,
                EffectLedgerModel.effect_key == effect_key,
                EffectLedgerModel.status == "reserved",
            )
            .values(
                status="failed",
                error=error[:2000],
                **({"error_type": error_type[:200]} if error_type else {}),
            )
            .returning(*_EFFECT_COLUMNS)
        )
        stmt_existing = select(*_EFFECT_COLUMNS).where(
            EffectLedgerModel.organization_id == self._tenant.organization_id,
            EffectLedgerModel.effect_key == effect_key,
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt_update)
            row = await cursor.fetchone()
            if row is not None:
                if release_quota:
                    await self._database.execute(
                        connection,
                        update(ProviderQuota)
                        .where(
                            ProviderQuota.organization_id == self._tenant.organization_id,
                            ProviderQuota.provider == row[2],
                        )
                        .values(used_units=ProviderQuota.used_units - int(row[3])),
                    )
            else:
                existing_cursor = await self._database.execute(
                    connection,
                    stmt_existing,
                )
                row = await existing_cursor.fetchone()
                if row is None:
                    raise ValueError(f"efeito {effect_key!r} inexistente")
                if row[4] != "failed":
                    raise UncertainEffectError(
                        f"efeito {effect_key!r} não pode falhar a partir de {row[4]!r}"
                    )
        return _reservation(row, created=False)

    async def mark_reconciled(
        self,
        effect_key: str,
        *,
        result: dict[str, Any],
    ) -> EffectReservation:
        """Resolve an uncertain effect only from a unique provider-side match."""
        stmt = (
            update(EffectLedgerModel)
            .where(
                EffectLedgerModel.organization_id == self._tenant.organization_id,
                EffectLedgerModel.effect_key == effect_key,
                EffectLedgerModel.status == "uncertain",
            )
            .values(
                status="succeeded",
                result=result,
                provider_status="succeeded",
            )
            .returning(*_EFFECT_COLUMNS)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
            if row is None:
                raise UncertainEffectError(
                    f"efeito {effect_key!r} não está disponível para reconciliação"
                )
        return _reservation(row, created=False)

    async def quota_usage(self, provider: str) -> tuple[int, int]:
        stmt = select(ProviderQuota.used_units, ProviderQuota.limit_units).where(
            ProviderQuota.organization_id == self._tenant.organization_id,
            ProviderQuota.provider == provider,
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"quota de {provider!r} inexistente")
        return row
