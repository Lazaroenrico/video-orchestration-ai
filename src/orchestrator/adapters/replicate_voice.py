"""ReplicateVoiceAdapter — cria referência de voz de creator via Replicate.

Contrato HTTP (Replicate v1 simplificado):
POST ``{base_url}/predictions``

Request body::

    {
        "model": "suno-ai/bark",
        "input": {"prompt": "creator voice {index}"}
    }

Response JSON::

    {
        "id": "<prediction-id>",
        "status": "starting",
        "output": null
    }

``create_voice`` retorna o ``id`` da prediction como ``voice_id``.
Esse ID pode ser usado futuramente para buscar o áudio gerado via GET /predictions/{id}.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

_VOICE_MODEL = "suno-ai/bark"


class ReplicateVoiceAdapter:
    """Cria referência de voz sintética via Replicate.

    Parameters
    ----------
    base_url:
        Base da API. Padrão: ``https://api.replicate.com/v1``.
    token:
        Token (``Authorization: Token <token>``). Se vazio, lê ``REPLICATE_API_TOKEN``.
    client:
        ``httpx.AsyncClient`` injetável para testes.
    """

    def __init__(
        self,
        base_url: str = "https://api.replicate.com/v1",
        token: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("REPLICATE_API_TOKEN", "")
        self._client = client

    async def create_voice(self, index: int) -> str:
        """Inicia geração de voz para o creator ``index``. Retorna prediction ID."""
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }
        body = {
            "model": _VOICE_MODEL,
            "input": {"prompt": f"creator voice {index}"},
        }

        if self._client is not None:
            resp = await self._client.post(
                f"{self.base_url}/predictions",
                headers=headers,
                json=body,
            )
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/predictions",
                    headers=headers,
                    json=body,
                )

        resp.raise_for_status()
        data = resp.json()
        return data["id"]
