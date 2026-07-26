"""Classificação de retenção no fluxo de QC (D30, Fase 5).

A classe de retenção só é decidível **depois** do veredito: quando o clip é persistido o
QC ainda não rodou. Aqui provamos que o QC aprovado promove a take final e rebaixa as
anteriores, e que o drop marca tudo como reprovado.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.graph.state import Artifact, Item, QCResult
from orchestrator.nodes.stages import classify_item_retention
from orchestrator.storage.db import ArtifactDB, ArtifactRecord
from orchestrator.storage.retention import (
    RETENTION_INTERMEDIATE,
    RETENTION_KEEP,
    RETENTION_REJECTED,
)

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path) -> ArtifactDB:
    store = ArtifactDB(tmp_path / "artifacts.sqlite")
    store.setup()
    return store


def _clip(n: int) -> Artifact:
    return Artifact(
        kind="clip",
        uri=f"/videos/run-1/items/item-0/clip-{n}.mp4",
        meta={"storage_key": f"run-1/items/item-0/clip-{n}.mp4", "storage_backend": "local"},
    )


async def _seed(db: ArtifactDB, n: int) -> str:
    key = f"run-1/items/item-0/clip-{n}.mp4"
    await db.record(
        ArtifactRecord(
            run_id="run-1", kind="clip", storage_backend="local", storage_key=key, item_id="item-0",
        )
    )
    return key


async def test_an_approved_item_keeps_only_its_final_take(db):
    """A take que passou é o entregável; as anteriores são tentativas intermediárias."""
    for n in range(3):
        await _seed(db, n)
    item = Item(id="item-0", concept={}, clips=[_clip(0), _clip(1), _clip(2)], qc=QCResult(passed=True, score=0.9))

    await classify_item_retention(item, db=db, now=_NOW)

    rows = {r.storage_key: r for r in await db.by_run("run-1")}
    assert rows["run-1/items/item-0/clip-2.mp4"].retention_class == RETENTION_KEEP
    assert rows["run-1/items/item-0/clip-2.mp4"].expires_at is None
    for n in (0, 1):
        row = rows[f"run-1/items/item-0/clip-{n}.mp4"]
        assert row.retention_class == RETENTION_INTERMEDIATE
        assert row.expires_at == (_NOW + timedelta(days=2)).isoformat()


async def test_a_dropped_item_marks_every_clip_as_rejected(db):
    for n in range(2):
        await _seed(db, n)
    item = Item(id="item-0", concept={}, clips=[_clip(0), _clip(1)], dropped=True)

    await classify_item_retention(item, db=db, now=_NOW)

    for row in await db.by_run("run-1"):
        assert row.retention_class == RETENTION_REJECTED
        assert row.expires_at == (_NOW + timedelta(days=3)).isoformat()


async def test_an_item_still_in_flight_is_not_classified(db):
    """QC reprovou mas ainda há tentativa pela frente: cedo demais para condenar bytes."""
    await _seed(db, 0)
    item = Item(id="item-0", concept={}, clips=[_clip(0)], qc=QCResult(passed=False, score=0.1))

    await classify_item_retention(item, db=db, now=_NOW)

    assert (await db.by_run("run-1"))[0].retention_class == RETENTION_KEEP


async def test_classification_is_a_noop_without_a_db(db):
    item = Item(id="item-0", concept={}, clips=[_clip(0)], qc=QCResult(passed=True, score=0.9))

    await classify_item_retention(item, db=None, now=_NOW)  # não levanta


async def test_a_clip_without_a_storage_pointer_is_skipped(db):
    """mock:// nunca virou objeto: não há key para reclassificar."""
    item = Item(
        id="item-0", concept={},
        clips=[Artifact(kind="clip", uri="mock://video/abc")],
        qc=QCResult(passed=True, score=0.9),
    )

    await classify_item_retention(item, db=db, now=_NOW)  # não levanta

    assert await db.by_run("run-1") == []
