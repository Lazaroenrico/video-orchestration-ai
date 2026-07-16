"""Política de retenção por metadata (D30, Fase 5).

D30: creator assets, clips aprovados e vídeos finais são retidos; clips reprovados
expiram em 3 dias; tentativas intermediárias em 2 dias. A limpeza é orientada pelo DB,
**não** por varredura cega do bucket.

Determinístico: ``now`` é sempre injetado — nada de ``datetime.now()`` implícito.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.storage.db import ArtifactDB, ArtifactRecord
from orchestrator.storage.local import LocalMediaStorage
from orchestrator.storage.retention import (
    RETENTION_INTERMEDIATE,
    RETENTION_KEEP,
    RETENTION_REJECTED,
    expires_at_for,
    purge_expired,
)

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path) -> ArtifactDB:
    store = ArtifactDB(tmp_path / "artifacts.sqlite")
    store.setup()
    return store


# --------------------------------------------------------------------------- #
# Política                                                                     #
# --------------------------------------------------------------------------- #


def test_kept_artifacts_never_get_an_expiry():
    """Creator assets, clips aprovados e vídeos finais são retidos (D30)."""
    assert expires_at_for(RETENTION_KEEP, now=_NOW) is None


def test_a_rejected_clip_expires_in_three_days():
    assert expires_at_for(RETENTION_REJECTED, now=_NOW) == (_NOW + timedelta(days=3)).isoformat()


def test_an_intermediate_attempt_expires_in_two_days():
    assert expires_at_for(RETENTION_INTERMEDIATE, now=_NOW) == (_NOW + timedelta(days=2)).isoformat()


def test_an_unknown_retention_class_fails_loudly():
    """Classe nova sem TTL não pode virar 'nunca expira' por omissão."""
    with pytest.raises(ValueError, match="unknown retention class"):
        expires_at_for("whatever", now=_NOW)


# --------------------------------------------------------------------------- #
# set_retention                                                                #
# --------------------------------------------------------------------------- #


async def test_set_retention_reclassifies_an_artifact_and_stamps_the_expiry(db):
    """O QC roda depois da persistência: a classe só pode ser decidida a posteriori."""
    record = ArtifactRecord(
        run_id="run-1", kind="clip", storage_backend="local", storage_key="run-1/clip-0.mp4",
    )
    await db.record(record)

    await db.set_retention("run-1/clip-0.mp4", RETENTION_REJECTED, now=_NOW)

    got = await db.by_key("run-1/clip-0.mp4")
    assert got.retention_class == RETENTION_REJECTED
    assert got.expires_at == (_NOW + timedelta(days=3)).isoformat()


async def test_set_retention_back_to_keep_clears_the_expiry(db):
    """Promover uma tentativa a clip aprovado tem de apagar o expires_at anterior."""
    await db.record(
        ArtifactRecord(
            run_id="run-1", kind="clip", storage_backend="local", storage_key="run-1/clip-0.mp4",
            retention_class=RETENTION_REJECTED, expires_at="2026-07-19T00:00:00+00:00",
        )
    )

    await db.set_retention("run-1/clip-0.mp4", RETENTION_KEEP, now=_NOW)

    got = await db.by_key("run-1/clip-0.mp4")
    assert got.retention_class == RETENTION_KEEP
    assert got.expires_at is None


async def test_set_retention_on_an_unknown_key_is_a_noop(db):
    await db.set_retention("run-1/ghost.mp4", RETENTION_REJECTED, now=_NOW)

    assert await db.by_key("run-1/ghost.mp4") is None


# --------------------------------------------------------------------------- #
# Consulta de expirados                                                        #
# --------------------------------------------------------------------------- #


async def _seed(db, key, retention, expires_at):
    await db.record(
        ArtifactRecord(
            run_id="run-1", kind="clip", storage_backend="local", storage_key=key,
            retention_class=retention, expires_at=expires_at,
        )
    )


async def test_expired_returns_only_artifacts_past_their_expiry(db):
    await _seed(db, "past.mp4", RETENTION_REJECTED, (_NOW - timedelta(days=1)).isoformat())
    await _seed(db, "future.mp4", RETENTION_REJECTED, (_NOW + timedelta(days=1)).isoformat())
    await _seed(db, "kept.mp4", RETENTION_KEEP, None)

    rows = await db.expired(now=_NOW)

    assert [r.storage_key for r in rows] == ["past.mp4"]


async def test_a_kept_artifact_is_never_expired_however_old(db):
    """Sem expires_at não há expiração — é o invariante que protege o vídeo final."""
    await _seed(db, "kept.mp4", RETENTION_KEEP, None)

    assert await db.expired(now=_NOW + timedelta(days=3650)) == []


# --------------------------------------------------------------------------- #
# purge_expired                                                                #
# --------------------------------------------------------------------------- #


async def test_purge_deletes_the_bytes_and_the_row_for_expired_artifacts(tmp_path, db):
    storage = LocalMediaStorage(tmp_path, web_prefix="/videos")
    stored = await storage.put_bytes(b"old", key_base="run-1/clip-0", content_type="video/mp4")
    await _seed(db, stored.key, RETENTION_REJECTED, (_NOW - timedelta(days=1)).isoformat())

    purged = await purge_expired(db, storage, now=_NOW)

    assert purged == [stored.key]
    assert await storage.exists(stored.key) is False
    assert await db.by_key(stored.key) is None


async def test_purge_leaves_kept_artifacts_alone(tmp_path, db):
    storage = LocalMediaStorage(tmp_path, web_prefix="/videos")
    stored = await storage.put_bytes(b"final", key_base="run-1/assembled", content_type="video/mp4")
    await _seed(db, stored.key, RETENTION_KEEP, None)

    assert await purge_expired(db, storage, now=_NOW) == []
    assert await storage.exists(stored.key) is True


async def test_purge_is_driven_by_the_db_not_by_scanning_the_bucket(tmp_path, db):
    """D30 é explícita: limpeza por metadata, não varredura cega. Bytes sem linha ficam."""
    storage = LocalMediaStorage(tmp_path, web_prefix="/videos")
    orphan = await storage.put_bytes(b"orphan", key_base="run-1/unknown", content_type="video/mp4")

    assert await purge_expired(db, storage, now=_NOW) == []
    assert await storage.exists(orphan.key) is True
