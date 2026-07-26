"""Ledger tenant-scoped para quotas e efeitos externos idempotentes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from orchestrator.db.database import Database
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


_EFFECT_COLUMNS = """
    effect_key, run_id, provider, units, status, request, result, error
"""


class PostgresEffectLedger:
    """Reserva custo antes do side effect e impede reemissão ambígua."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def set_quota(self, provider: str, *, limit_units: int) -> None:
        if limit_units < 0:
            raise ValueError("quota não pode ser negativa")
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO provider_quotas (
                    organization_id, provider, limit_units
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (organization_id, provider) DO UPDATE
                SET limit_units = EXCLUDED.limit_units,
                    updated_at = CURRENT_TIMESTAMP
                WHERE provider_quotas.used_units <= EXCLUDED.limit_units
                RETURNING provider
                """,
                (self._tenant.organization_id, provider, limit_units),
            )
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
        async with self._database.connection(self._tenant) as connection:
            quota_cursor = await connection.execute(
                """
                SELECT limit_units, used_units
                FROM provider_quotas
                WHERE organization_id = %s AND provider = %s
                FOR UPDATE
                """,
                (self._tenant.organization_id, provider),
            )
            quota = await quota_cursor.fetchone()
            if quota is None:
                raise QuotaExceededError(
                    f"quota não configurada para provider {provider!r}"
                )

            existing_cursor = await connection.execute(
                f"""
                SELECT {_EFFECT_COLUMNS}
                FROM external_effects
                WHERE organization_id = %s AND effect_key = %s
                """,
                (self._tenant.organization_id, effect_key),
            )
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
            await connection.execute(
                """
                INSERT INTO external_effects (
                    organization_id, effect_key, run_id, provider, units,
                    status, request
                )
                VALUES (%s, %s, %s, %s, %s, 'reserved', %s)
                """,
                (
                    self._tenant.organization_id,
                    effect_key,
                    run_id,
                    provider,
                    units,
                    Jsonb(request),
                ),
            )
            await connection.execute(
                """
                UPDATE provider_quotas
                SET used_units = used_units + %s, updated_at = CURRENT_TIMESTAMP
                WHERE organization_id = %s AND provider = %s
                """,
                (units, self._tenant.organization_id, provider),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                UPDATE external_effects
                SET status = 'uncertain', error = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE organization_id = %s AND effect_key = %s
                RETURNING {_EFFECT_COLUMNS}
                """,
                (error[:2000], self._tenant.organization_id, effect_key),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                UPDATE external_effects
                SET status = 'succeeded', result = %s, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE organization_id = %s AND effect_key = %s
                  AND status = 'reserved'
                RETURNING {_EFFECT_COLUMNS}
                """,
                (Jsonb(result), self._tenant.organization_id, effect_key),
            )
            row = await cursor.fetchone()
            if row is None:
                existing_cursor = await connection.execute(
                    f"""
                    SELECT {_EFFECT_COLUMNS}
                    FROM external_effects
                    WHERE organization_id = %s AND effect_key = %s
                    """,
                    (self._tenant.organization_id, effect_key),
                )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT used_units, limit_units
                FROM provider_quotas
                WHERE organization_id = %s AND provider = %s
                """,
                (self._tenant.organization_id, provider),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"quota de {provider!r} inexistente")
        return row
