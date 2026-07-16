"""Ligação storage + ArtifactDB na persistência de mídia (D30, Fase 3.5).

É aqui que a D30 deixa de ser infra e vira comportamento: os bytes vão para o backend
configurado e a metadata canônica cai no DB relacional. Offline e determinístico — o
backend é o local em tmp_path e o DB é SQLite em tmp_path.
"""
from __future__ import annotations

import base64

import pytest

from orchestrator import media_store
from orchestrator.graph.state import Artifact, Item
from orchestrator.storage.db import ArtifactDB
from orchestrator.storage.local import LocalMediaStorage

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()
_MP4_DATA_URI = "data:video/mp4;base64," + base64.b64encode(b"\x00mp4").decode()


@pytest.fixture
def db(tmp_path) -> ArtifactDB:
    store = ArtifactDB(tmp_path / "artifacts.sqlite")
    store.setup()
    return store


# --------------------------------------------------------------------------- #
# persist_item_media                                                           #
# --------------------------------------------------------------------------- #


async def test_persisting_an_item_records_its_clips_in_the_db(tmp_path, db):
    item = Item(id="item-0", concept={"hook": "h"}, clips=[Artifact(kind="clip", uri=_MP4_DATA_URI)])

    await media_store.persist_item_media(item, run_id="run-1", videos_root=tmp_path, db=db)

    rows = await db.by_run("run-1")
    assert len(rows) == 1
    assert rows[0].kind == "clip"
    assert rows[0].item_id == "item-0"
    assert rows[0].storage_key == "run-1/items/item-0/clip-0.mp4"
    assert rows[0].storage_backend == "local"
    assert rows[0].content_type == "video/mp4"
    assert rows[0].size_bytes == 4


async def test_the_db_keeps_the_original_uri_as_provenance(tmp_path, db):
    """source_uri é o que prova de onde o byte veio — replicate.delivery expira em ~1h."""
    item = Item(id="item-0", concept={}, clips=[Artifact(kind="clip", uri=_MP4_DATA_URI)])

    await media_store.persist_item_media(item, run_id="run-1", videos_root=tmp_path, db=db)

    assert (await db.by_run("run-1"))[0].source_uri == _MP4_DATA_URI


async def test_the_assembled_video_is_recorded_with_the_kind_the_artifact_declares(tmp_path, db):
    """O Artifact já carrega kind; o DB não deve inventar um vocabulário paralelo."""
    item = Item(id="item-0", concept={}, assembled=Artifact(kind="video", uri=_MP4_DATA_URI))

    await media_store.persist_item_media(item, run_id="run-1", videos_root=tmp_path, db=db)

    rows = await db.by_run("run-1")
    assert [r.kind for r in rows] == ["video"]
    assert rows[0].storage_key == "run-1/items/item-0/assembled.mp4"


async def test_nothing_is_recorded_for_a_non_downloadable_clip(tmp_path, db):
    """mock:// é referência, não bytes: sem objeto, não há artifact para registrar."""
    item = Item(id="item-0", concept={}, clips=[Artifact(kind="clip", uri="mock://video/abc")])

    await media_store.persist_item_media(item, run_id="run-1", videos_root=tmp_path, db=db)

    assert await db.by_run("run-1") == []


async def test_persisting_without_a_db_still_rewrites_the_uris(tmp_path):
    """O caminho offline atual não depende do DB — passar db é opcional."""
    item = Item(id="item-0", concept={}, clips=[Artifact(kind="clip", uri=_MP4_DATA_URI)])

    out = await media_store.persist_item_media(item, run_id="run-1", videos_root=tmp_path)

    assert out.clips[0].uri == "/videos/run-1/items/item-0/clip-0.mp4"


async def test_an_injected_storage_backend_is_used_instead_of_the_root(tmp_path, db):
    """É o gancho do R2: quem chama passa o backend, não um diretório."""
    storage = LocalMediaStorage(tmp_path / "custom", web_prefix="/videos")
    item = Item(id="item-0", concept={}, clips=[Artifact(kind="clip", uri=_MP4_DATA_URI)])

    await media_store.persist_item_media(item, run_id="run-1", storage=storage, db=db)

    assert (tmp_path / "custom/run-1/items/item-0/clip-0.mp4").is_file()
    assert (await db.by_run("run-1"))[0].storage_backend == "local"


async def test_persist_item_media_still_requires_a_root_or_a_storage(tmp_path):
    with pytest.raises(TypeError):
        await media_store.persist_item_media({"id": "x"}, run_id="run-1")


# --------------------------------------------------------------------------- #
# persist_creator_media                                                        #
# --------------------------------------------------------------------------- #


async def test_persisting_a_creator_records_image_and_voice_with_their_kinds(tmp_path, db):
    creator = {"id": "creator-0", "upscaled_base": _PNG_DATA_URI, "voice_id": _PNG_DATA_URI}

    await media_store.persist_creator_media(creator, run_id="run-1", media_root=tmp_path, db=db)

    rows = await db.by_run("run-1")
    assert {r.kind for r in rows} == {"image", "voice"}
    assert all(r.creator_id == "creator-0" for r in rows)
    assert all(r.item_id is None for r in rows)


async def test_a_creator_voice_that_is_only_an_id_is_not_recorded(tmp_path, db):
    """voice-0 do ElevenLabs é referência opaca: não há bytes nossos para registrar."""
    creator = {"id": "creator-0", "upscaled_base": _PNG_DATA_URI, "voice_id": "voice-0"}

    await media_store.persist_creator_media(creator, run_id="run-1", media_root=tmp_path, db=db)

    assert [r.kind for r in await db.by_run("run-1")] == ["image"]


async def test_creator_assets_default_to_the_keep_retention_class(tmp_path, db):
    """D30: creator assets são retidos — nunca ganham expires_at automático."""
    creator = {"id": "creator-0", "upscaled_base": _PNG_DATA_URI}

    await media_store.persist_creator_media(creator, run_id="run-1", media_root=tmp_path, db=db)

    row = (await db.by_run("run-1"))[0]
    assert row.retention_class == "keep"
    assert row.expires_at is None
