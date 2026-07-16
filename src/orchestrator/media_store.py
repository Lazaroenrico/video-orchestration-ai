"""Persistência dos bytes de mídia de creators e items.

Por que existe: imagem upscalada (Replicate/Topaz) e áudio remoto vêm como
URLs voláteis — ``replicate.delivery`` expira em ~1h. Para não perder os artefatos,
persistimos os bytes e reescrevemos as URIs do creator/item para um caminho servível,
guardando a URL original como ``*_source_uri`` para proveniência.

Desde a D30 este módulo é a camada de **orquestração** por cima de
``orchestrator.storage``: ele decide *o que* persistir e sob qual key canônica
(``{run_id}/{creator_id}/image``, ``{run_id}/items/{item_id}/clip-{n}``), enquanto o
backend decide *onde* os bytes moram. Hoje o backend é sempre ``LocalMediaStorage``;
o ``R2MediaStorage`` entra sem que estas funções mudem.

Determinismo (CLAUDE.md): ``mock://...`` e ids de voz (``voice-0``) **não** são
baixáveis — a persistência é no-op e retorna a uri inalterada, sem tocar disco nem
rede. Por isso a suíte mock continua offline e determinística.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional

import httpx

from orchestrator.storage.base import (
    _DEFAULT_EXT,
    MediaStorage,
    StoredObject,
    ext_from_mime as _ext_from_mime,
    ext_from_url as _ext_from_url,
    is_downloadable as _is_downloadable,
)
from orchestrator.storage.db import ArtifactDB, ArtifactRecord
from orchestrator.storage.local import LocalMediaStorage

_log = logging.getLogger(__name__)

__all__ = [
    "data_uri_from_media_path",
    "persist_bytes",
    "persist_creator_media",
    "persist_item_media",
    "persist_media",
]


def data_uri_from_media_path(uri: str, media_root: Path) -> Optional[str]:
    """Reconstrói um ``data:`` URI a partir de um arquivo servido em ``/media/...``.

    Um path web ``/media/{run}/{creator}/image.png`` é servível pelo dashboard, mas
    NÃO é acessível por um provider externo (Replicate etc.). Ao reutilizar um creator
    cuja única referência de imagem é esse path local, o provider precisa dos bytes:
    lê o arquivo do disco (``media_root`` + resto do path) e devolve um data URI.

    Retorna ``None`` quando ``uri`` não é um path ``/media/`` local, ou o arquivo
    não existe — nesses casos não há o que reconstruir.
    """
    if not isinstance(uri, str) or not uri.startswith("/media/"):
        return None
    rel = uri[len("/media/"):]
    path = media_root / rel
    if not path.is_file():
        return None
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


async def persist_media(
    uri: str,
    dest_dir: str | Path,
    basename: str,
    *,
    web_prefix: str,
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Baixa os bytes de ``uri`` para ``dest_dir/{basename}.{ext}``.

    Retorna o caminho web servível ``{web_prefix}/{basename}.{ext}``. Para uris não
    baixáveis (``mock://``, voice_id) ou em falha de download, retorna ``uri``
    inalterada — nunca levanta, nunca cria diretório à toa.
    """
    storage = LocalMediaStorage(dest_dir, web_prefix=web_prefix)
    stored = await storage.put_from_url(uri, key_base=basename, client=client)
    return stored.uri if stored else uri


async def persist_bytes(
    data: bytes,
    dest_dir: str | Path,
    basename: str,
    *,
    web_prefix: str,
    content_type: str = "audio/mpeg",
) -> str:
    """Grava ``data`` em ``dest_dir/{basename}.{ext}`` e retorna o caminho web servível.

    Usado quando os bytes já estão em mãos (ex.: TTS síncrono de preview de voz) e
    não há uri para baixar via ``persist_media``.
    """
    storage = LocalMediaStorage(dest_dir, web_prefix=web_prefix)
    stored = await storage.put_bytes(data, key_base=basename, content_type=content_type)
    return stored.uri


async def _record(
    db: Optional[ArtifactDB],
    stored: StoredObject,
    *,
    run_id: str,
    kind: str,
    source_uri: str,
    item_id: Optional[str] = None,
    creator_id: Optional[str] = None,
) -> None:
    """Grava a metadata canônica do objeto no DB, quando há DB.

    ``db`` é opcional de propósito: o dry-run e a suíte offline não precisam de DB, e
    exigi-lo tornaria o caminho mock dependente de estado transacional que ele não tem.
    """
    if db is None:
        return
    await db.record(
        ArtifactRecord.from_stored(
            stored,
            run_id=run_id,
            kind=kind,
            item_id=item_id,
            creator_id=creator_id,
            source_uri=source_uri,
        )
    )


def _storage_for(
    storage: Optional[MediaStorage],
    root: str | Path | None,
    *,
    web_prefix: str,
    required_arg: str,
) -> MediaStorage:
    """Backend injetado vence; sem ele, cai no disco local sob ``root``."""
    if storage is not None:
        return storage
    if root is None:
        raise TypeError(f"exige {required_arg} ou storage")
    return LocalMediaStorage(root, web_prefix=web_prefix)


async def persist_item_media(
    item: Any,
    *,
    run_id: str,
    videos_root: str | Path | None = None,
    media_root: str | Path | None = None,
    client: Optional[httpx.AsyncClient] = None,
    storage: Optional[MediaStorage] = None,
    db: Optional[ArtifactDB] = None,
) -> Any:
    """Persiste os bytes dos clips e do vídeo montado de um ``Item``.

    - ``clips[n].uri`` http(s)/data: -> persistido sob a key
      ``{run_id}/items/{item_id}/clip-{n}.{ext}``, servido em ``/videos/...``; a uri
      original fica em ``meta["source_uri"]``.
    - ``assembled.uri`` http(s)/data: -> mesma coisa sob ``assembled``.
    - Não-baixáveis (``mock://``, ids opacos): no-op total, item devolvido inalterado.

    Aceita ``item`` como ``Item`` (pydantic) ou dict — devolve o mesmo tipo recebido,
    mirando o padrão já usado em ``persist_creator_media``/``_to_plain`` do server.
    """
    root = videos_root if videos_root is not None else media_root
    backend = _storage_for(storage, root, web_prefix="/videos", required_arg="videos_root")

    is_model = hasattr(item, "model_dump")
    data = item.model_dump() if is_model else dict(item)
    item_id = data.get("id") or "item"
    key_prefix = f"{run_id}/items/{item_id}"

    clips = data.get("clips") or []
    new_clips: list[dict[str, Any]] = []
    for n, clip in enumerate(clips):
        clip = dict(clip)
        uri = clip.get("uri")
        if isinstance(uri, str):
            stored = await backend.put_from_url(uri, key_base=f"{key_prefix}/clip-{n}", client=client)
            if stored:
                meta = dict(clip.get("meta") or {})
                meta["source_uri"] = uri
                clip = {**clip, "uri": stored.uri, "meta": meta}
                await _record(
                    db, stored, run_id=run_id, kind=clip.get("kind") or "clip",
                    source_uri=uri, item_id=item_id,
                )
        new_clips.append(clip)
    data["clips"] = new_clips

    assembled = data.get("assembled")
    if assembled:
        assembled = dict(assembled)
        uri = assembled.get("uri")
        if isinstance(uri, str):
            stored = await backend.put_from_url(uri, key_base=f"{key_prefix}/assembled", client=client)
            if stored:
                meta = dict(assembled.get("meta") or {})
                meta["source_uri"] = uri
                assembled = {**assembled, "uri": stored.uri, "meta": meta}
                await _record(
                    db, stored, run_id=run_id, kind=assembled.get("kind") or "video",
                    source_uri=uri, item_id=item_id,
                )
        data["assembled"] = assembled

    if is_model:
        return type(item).model_validate(data)
    return data


async def persist_creator_media(
    creator: dict[str, Any],
    *,
    run_id: str,
    media_root: str | Path | None = None,
    client: Optional[httpx.AsyncClient] = None,
    storage: Optional[MediaStorage] = None,
    db: Optional[ArtifactDB] = None,
) -> dict[str, Any]:
    """Persiste imagem e voz (quando baixáveis) e reescreve as URIs do creator.

    - ``upscaled_base`` http(s)/data: -> persistido; URI vira caminho servível e
      ``image_source_uri`` guarda a origem.
    - ``voice_id`` http(s) (ex.: ElevenLabs via Replicate) -> persistido como áudio;
      ``voice_source_uri`` guarda a origem. Um ``voice_id`` que é só id (ElevenLabs)
      não é baixável -> permanece referência intacta.
    - Mock (``mock://``, ``voice-0``): no-op total, dict devolvido inalterado.
    """
    creator_id = creator.get("id") or "creator"
    backend = _storage_for(storage, media_root, web_prefix="/media", required_arg="media_root")
    key_prefix = f"{run_id}/{creator_id}"
    out = dict(creator)

    image_uri = out.get("upscaled_base")
    if isinstance(image_uri, str):
        stored = await backend.put_from_url(image_uri, key_base=f"{key_prefix}/image", client=client)
        if stored:
            out["upscaled_base"] = stored.uri
            out["image_source_uri"] = image_uri
            if media_root is not None:
                out["image_local_path"] = str(Path(media_root) / stored.key)
            await _record(
                db, stored, run_id=run_id, kind="image", source_uri=image_uri, creator_id=creator_id,
            )

    voice_uri = out.get("voice_id")
    if isinstance(voice_uri, str):
        stored = await backend.put_from_url(voice_uri, key_base=f"{key_prefix}/voice", client=client)
        if stored:
            out["voice_id"] = stored.uri
            out["voice_source_uri"] = voice_uri
            await _record(
                db, stored, run_id=run_id, kind="voice", source_uri=voice_uri, creator_id=creator_id,
            )

    return out
