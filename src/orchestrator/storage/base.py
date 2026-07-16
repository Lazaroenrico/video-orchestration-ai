"""Contrato de storage de mídia e helpers de mime/extensão (D30).

``StoredObject`` é o retorno canônico de toda escrita: ``key`` é o ponteiro para o
objeto (o que o DB persiste, D30), ``uri`` é o que vai para o ``Artifact`` e
``sha256``/``size_bytes`` dão integridade e auditoria. Signed URLs são derivadas de
``key`` sob demanda e nunca persistidas como verdade.
"""
from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

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
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def is_downloadable(uri: str) -> bool:
    """True só para http(s) e data: — o resto (mock://, voice_id, "") é referência."""
    if not uri:
        return False
    if uri.startswith("data:"):
        return True
    return urlparse(uri).scheme in {"http", "https"}


def ext_from_mime(content_type: str) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime]
    # Fallback para mimes conhecidos do stdlib antes de degradar para .bin — evita
    # servir imagem/áudio como application/octet-stream (browser não renderiza).
    guessed = mimetypes.guess_extension(mime) if mime else None
    return guessed.lstrip(".") if guessed else _DEFAULT_EXT


def ext_from_url(uri: str) -> Optional[str]:
    path = urlparse(uri).path.lower()
    if "." in path:
        ext = path.rsplit(".", 1)[-1]
        if ext and len(ext) <= 5 and ext.isalnum():
            return ext
    return None


def decode_data_uri(uri: str) -> tuple[bytes, str]:
    """Decodifica ``data:<mime>;base64,<payload>`` -> (bytes, mime)."""
    header, _, payload = uri.partition(",")
    mime = _DEFAULT_CONTENT_TYPE
    if header.startswith("data:"):
        mime = header[len("data:"):].split(";", 1)[0] or mime
    data = base64.b64decode(payload) if ";base64" in header else payload.encode("utf-8")
    return data, mime


@dataclass(frozen=True)
class StoredObject:
    """Metadata canônica de um objeto persistido. Espelha as colunas da D30."""

    backend: str
    key: str
    uri: str
    content_type: str
    size_bytes: int
    sha256: str


@runtime_checkable
class MediaStorage(Protocol):
    """Contrato mínimo da D30. Implementado por ``LocalMediaStorage`` e ``R2MediaStorage``."""

    backend: str

    async def put_bytes(self, data: bytes, *, key_base: str, content_type: str) -> StoredObject:
        """Persiste ``data`` sob ``{key_base}.{ext}``, com ext derivada de ``content_type``."""
        ...

    async def put_from_url(
        self,
        uri: str,
        *,
        key_base: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Optional[StoredObject]:
        """Baixa ``uri`` e persiste sob ``{key_base}.{ext}``.

        Devolve ``None`` — nunca levanta — para uri não baixável (``mock://``, voice_id)
        ou falha de download, deixando o caller manter a referência original.
        """
        ...

    async def get_signed_url(self, key: str, *, ttl_seconds: int = 900) -> str:
        """URL de acesso aos bytes, derivada sob demanda. Não é valor canônico."""
        ...

    async def delete(self, key: str) -> None:
        """Remove o objeto. Idempotente: remover o que não existe não levanta."""
        ...

    async def exists(self, key: str) -> bool:
        ...
