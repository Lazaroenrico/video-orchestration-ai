"""Download e persistência local dos bytes de mídia do creator.

Por que existe: imagem upscalada (Replicate/Topaz) e áudio (Replicate bark) vêm como
URLs voláteis — ``replicate.delivery`` expira em ~1h. Para não perder os artefatos,
baixamos os bytes para ``ORCH_MEDIA`` (default ``.orchestrator/media``) e reescrevemos
as URIs do creator para um caminho web servível (``/media/{run_id}/{creator_id}/...``),
guardando a URL original como ``*_source_uri`` para proveniência.

Determinismo (CLAUDE.md): ``mock://...`` e ids de voz (``voice-0``) **não** são
baixáveis — ``persist_media`` é no-op e retorna a uri inalterada, sem tocar disco nem
rede. Por isso a suíte mock continua offline e determinística.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

_log = logging.getLogger(__name__)

# Extensão default por família de mime (fallback quando o content-type é desconhecido).
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
    "image/svg+xml": "svg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "video/mp4": "mp4",
    "video/webm": "webm",
}
_DEFAULT_EXT = "bin"


def _is_downloadable(uri: str) -> bool:
    """True só para http(s) e data: — o resto (mock://, voice_id, "") é referência."""
    if not uri:
        return False
    if uri.startswith("data:"):
        return True
    return urlparse(uri).scheme in {"http", "https"}


def _ext_from_mime(content_type: str) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime]
    # Fallback para mimes conhecidos do stdlib antes de degradar para .bin — evita
    # servir imagem/áudio como application/octet-stream (browser não renderiza).
    guessed = mimetypes.guess_extension(mime) if mime else None
    return guessed.lstrip(".") if guessed else _DEFAULT_EXT


def _ext_from_url(uri: str) -> Optional[str]:
    path = urlparse(uri).path.lower()
    if "." in path:
        ext = path.rsplit(".", 1)[-1]
        if ext and len(ext) <= 5 and ext.isalnum():
            return ext
    return None


def _decode_data_uri(uri: str) -> tuple[bytes, str]:
    """Decodifica ``data:<mime>;base64,<payload>`` -> (bytes, ext)."""
    header, _, payload = uri.partition(",")
    mime = "application/octet-stream"
    if header.startswith("data:"):
        mime = header[len("data:"):].split(";", 1)[0] or mime
    data = base64.b64decode(payload) if ";base64" in header else payload.encode("utf-8")
    return data, _ext_from_mime(mime)


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
    if not _is_downloadable(uri):
        return uri

    try:
        if uri.startswith("data:"):
            data, ext = _decode_data_uri(uri)
        else:
            owns_client = client is None
            client = client or httpx.AsyncClient(timeout=120.0)
            try:
                resp = await client.get(uri)
                resp.raise_for_status()
                data = resp.content
                ext = _ext_from_url(uri) or _ext_from_mime(resp.headers.get("content-type", ""))
            finally:
                if owns_client:
                    await client.aclose()
    except Exception as exc:  # noqa: BLE001 — download é best-effort
        _log.error("persist_media falhou para %s: %s: %s", uri, type(exc).__name__, exc)
        return uri

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{basename}.{ext}"
    (dest_dir / filename).write_bytes(data)
    return f"{web_prefix}/{filename}"


async def persist_bytes(
    data: bytes,
    dest_dir: str | Path,
    basename: str,
    *,
    web_prefix: str,
    ext: str = "mp3",
) -> str:
    """Grava ``data`` em ``dest_dir/{basename}.{ext}`` e retorna o caminho web servível.

    Usado quando os bytes já estão em mãos (ex.: TTS síncrono de preview de voz) e
    não há uri para baixar via ``persist_media``.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{basename}.{ext}"
    (dest_dir / filename).write_bytes(data)
    return f"{web_prefix}/{filename}"


async def persist_item_media(
    item: Any,
    *,
    run_id: str,
    media_root: str | Path,
    client: Optional[httpx.AsyncClient] = None,
) -> Any:
    """Persiste os bytes dos clips e do vídeo montado de um ``Item``.

    - ``clips[n].uri`` http(s)/data: -> baixado para
      ``/media/{run_id}/items/{item_id}/clip-{n}.{ext}``; a uri original fica em
      ``meta["source_uri"]``.
    - ``assembled.uri`` http(s)/data: -> baixado para
      ``/media/{run_id}/items/{item_id}/assembled.{ext}``, mesma proveniência.
    - Não-baixáveis (``mock://``, ids opacos): no-op total, item devolvido inalterado.

    Aceita ``item`` como ``Item`` (pydantic) ou dict — devolve o mesmo tipo recebido,
    mirando o padrão já usado em ``persist_creator_media``/``_to_plain`` do server.
    """
    is_model = hasattr(item, "model_dump")
    data = item.model_dump() if is_model else dict(item)
    item_id = data.get("id") or "item"
    dest_dir = Path(media_root) / run_id / "items" / item_id
    web_prefix = f"/media/{run_id}/items/{item_id}"

    clips = data.get("clips") or []
    new_clips: list[dict[str, Any]] = []
    for n, clip in enumerate(clips):
        clip = dict(clip)
        uri = clip.get("uri")
        if isinstance(uri, str) and _is_downloadable(uri):
            local = await persist_media(
                uri, dest_dir, f"clip-{n}", web_prefix=web_prefix, client=client,
            )
            if local != uri:
                meta = dict(clip.get("meta") or {})
                meta["source_uri"] = uri
                clip = {**clip, "uri": local, "meta": meta}
        new_clips.append(clip)
    data["clips"] = new_clips

    assembled = data.get("assembled")
    if assembled:
        assembled = dict(assembled)
        uri = assembled.get("uri")
        if isinstance(uri, str) and _is_downloadable(uri):
            local = await persist_media(
                uri, dest_dir, "assembled", web_prefix=web_prefix, client=client,
            )
            if local != uri:
                meta = dict(assembled.get("meta") or {})
                meta["source_uri"] = uri
                assembled = {**assembled, "uri": local, "meta": meta}
        data["assembled"] = assembled

    if is_model:
        return type(item).model_validate(data)
    return data


async def persist_creator_media(
    creator: dict[str, Any],
    *,
    run_id: str,
    media_root: str | Path,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Persiste imagem e voz (quando baixáveis) e reescreve as URIs do creator.

    - ``upscaled_base`` http(s)/data: -> baixado; URI vira caminho local e
      ``image_source_uri`` guarda a origem.
    - ``voice_id`` http(s) (ex.: Replicate bark) -> baixado como áudio;
      ``voice_source_uri`` guarda a origem. Um ``voice_id`` que é só id (ElevenLabs)
      não é baixável -> permanece referência intacta.
    - Mock (``mock://``, ``voice-0``): no-op total, dict devolvido inalterado.
    """
    creator_id = creator.get("id") or "creator"
    dest_dir = Path(media_root) / run_id / creator_id
    web_prefix = f"/media/{run_id}/{creator_id}"
    out = dict(creator)

    image_uri = out.get("upscaled_base")
    if isinstance(image_uri, str) and _is_downloadable(image_uri):
        local = await persist_media(
            image_uri, dest_dir, "image", web_prefix=web_prefix, client=client,
        )
        if local != image_uri:
            out["upscaled_base"] = local
            out["image_source_uri"] = image_uri

    voice_uri = out.get("voice_id")
    if isinstance(voice_uri, str) and _is_downloadable(voice_uri):
        local = await persist_media(
            voice_uri, dest_dir, "voice", web_prefix=web_prefix, client=client,
        )
        if local != voice_uri:
            out["voice_id"] = local
            out["voice_source_uri"] = voice_uri

    return out
