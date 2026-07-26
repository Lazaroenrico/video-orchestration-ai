"""Testes do DB relacional de artifacts (D30, Fase 2).

O DB é a fonte canônica de metadata e ponteiros; o storage guarda só bytes. SQLite-first
para preservar o modo offline — nenhum teste aqui toca rede ou credencial.
"""
from __future__ import annotations

import pytest

from orchestrator.storage.base import StoredObject
from orchestrator.storage.db import ArtifactDB, ArtifactRecord

_STORED = StoredObject(
    backend="local",
    key="run-1/items/item-0/clip-0.mp4",
    uri="/videos/run-1/items/item-0/clip-0.mp4",
    content_type="video/mp4",
    size_bytes=1024,
    sha256="a" * 64,
)


@pytest.fixture
def db(tmp_path) -> ArtifactDB:
    store = ArtifactDB(tmp_path / "artifacts.sqlite")
    store.setup()
    return store


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #


def test_setup_is_idempotent(tmp_path):
    store = ArtifactDB(tmp_path / "artifacts.sqlite")
    store.setup()
    store.setup()  # não levanta — startup repetido é normal


def test_setup_creates_the_parent_directory(tmp_path):
    store = ArtifactDB(tmp_path / "nested" / "deep" / "artifacts.sqlite")
    store.setup()
    assert (tmp_path / "nested" / "deep" / "artifacts.sqlite").exists()


# --------------------------------------------------------------------------- #
# record / get                                                                 #
# --------------------------------------------------------------------------- #


async def test_record_round_trips_every_canonical_field(db):
    record = ArtifactRecord(
        run_id="run-1",
        item_id="item-0",
        creator_id="creator-2",
        kind="clip",
        storage_backend="r2",
        storage_key="run-1/items/item-0/clip-0.mp4",
        content_type="video/mp4",
        size_bytes=1024,
        sha256="a" * 64,
        source_uri="https://replicate.delivery/xyz.mp4",
        retention_class="short_lived",
        expires_at="2026-07-19T00:00:00+00:00",
        meta={"tier": "ltx", "take": 2},
    )

    await db.record(record)
    got = await db.get(record.id)

    assert got == record
    assert got.meta == {"tier": "ltx", "take": 2}


async def test_get_returns_none_for_an_unknown_id(db):
    assert await db.get("nope") is None


async def test_record_derives_a_deterministic_id_from_run_and_key(db):
    """Determinismo (CLAUDE.md): nada de uuid4 — o id sai do ponteiro canônico."""
    a = ArtifactRecord(run_id="run-1", kind="clip", storage_backend="local", storage_key="k/clip-0.mp4")
    b = ArtifactRecord(run_id="run-1", kind="clip", storage_backend="local", storage_key="k/clip-0.mp4")
    c = ArtifactRecord(run_id="run-2", kind="clip", storage_backend="local", storage_key="k/clip-0.mp4")

    assert a.id == b.id
    assert a.id != c.id


async def test_recording_the_same_artifact_twice_upserts_instead_of_duplicating(db):
    """Um retry de stage re-persiste os mesmos bytes; isso não pode virar linha órfã."""
    first = ArtifactRecord(
        run_id="run-1", kind="clip", storage_backend="local",
        storage_key="run-1/items/item-0/clip-0.mp4", size_bytes=10,
    )
    await db.record(first)

    second = ArtifactRecord(
        run_id="run-1", kind="clip", storage_backend="local",
        storage_key="run-1/items/item-0/clip-0.mp4", size_bytes=999,
    )
    await db.record(second)

    rows = await db.by_run("run-1")
    assert len(rows) == 1
    assert rows[0].size_bytes == 999


async def test_optional_fields_default_to_none_and_survive_the_round_trip(db):
    record = ArtifactRecord(
        run_id="run-1", kind="image", storage_backend="local", storage_key="run-1/c0/image.png",
    )
    await db.record(record)

    got = await db.get(record.id)
    assert got.item_id is None
    assert got.creator_id is None
    assert got.content_type is None
    assert got.size_bytes is None
    assert got.sha256 is None
    assert got.source_uri is None
    assert got.expires_at is None
    assert got.retention_class == "keep"
    assert got.meta == {}


# --------------------------------------------------------------------------- #
# Consultas                                                                    #
# --------------------------------------------------------------------------- #


async def test_by_run_returns_only_that_run_ordered_by_key(db):
    for run, key in [("run-1", "b.mp4"), ("run-2", "z.mp4"), ("run-1", "a.mp4")]:
        await db.record(
            ArtifactRecord(run_id=run, kind="clip", storage_backend="local", storage_key=key)
        )

    rows = await db.by_run("run-1")

    assert [r.storage_key for r in rows] == ["a.mp4", "b.mp4"]


async def test_by_run_is_empty_for_an_unknown_run(db):
    assert await db.by_run("ghost") == []


async def test_by_key_finds_the_artifact_that_owns_a_storage_key(db):
    record = ArtifactRecord(
        run_id="run-1", kind="clip", storage_backend="r2", storage_key="run-1/items/item-0/clip-0.mp4",
    )
    await db.record(record)

    assert (await db.by_key("run-1/items/item-0/clip-0.mp4")).id == record.id
    assert await db.by_key("run-1/missing.mp4") is None


# --------------------------------------------------------------------------- #
# Ponte com o storage (Fase 1)                                                 #
# --------------------------------------------------------------------------- #


async def test_from_stored_carries_the_storage_metadata_into_the_record():
    """StoredObject é o que a Fase 1 devolve; o DB não deve reinventar esses campos."""
    record = ArtifactRecord.from_stored(
        _STORED,
        run_id="run-1",
        kind="clip",
        item_id="item-0",
        source_uri="https://replicate.delivery/xyz.mp4",
        meta={"tier": "ltx"},
    )

    assert record.storage_backend == "local"
    assert record.storage_key == "run-1/items/item-0/clip-0.mp4"
    assert record.content_type == "video/mp4"
    assert record.size_bytes == 1024
    assert record.sha256 == "a" * 64
    assert record.source_uri == "https://replicate.delivery/xyz.mp4"
    assert record.item_id == "item-0"
    assert record.meta == {"tier": "ltx"}


async def test_from_stored_defaults_retention_to_keep():
    assert ArtifactRecord.from_stored(_STORED, run_id="run-1", kind="clip").retention_class == "keep"
