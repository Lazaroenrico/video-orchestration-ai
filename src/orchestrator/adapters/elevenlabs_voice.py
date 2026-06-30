"""ElevenLabsVoiceAdapter — cria voz de creator via ElevenLabs API.

## Contrato HTTP assumido
POST ``{base_url}/voices/add``

Headers::

    xi-api-key: <token>
    Content-Type: application/json

Request body::

    {
        "name": "creator-{index}"
    }

Response JSON esperado::

    {
        "voice_id": "abc123..."
    }

``create_voice`` retorna a string ``voice_id`` diretamente.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

from orchestrator.tracing import traced


class ElevenLabsVoiceAdapter:
    """Cria voz sintética de creator via ElevenLabs.

    Parameters
    ----------
    base_url:
        Base da API. Padrão: ``https://api.elevenlabs.io/v1``.
    token:
        Token de autenticação (header ``xi-api-key``).
        Se vazio, lê de ``ELEVENLABS_API_KEY``.
    client:
        ``httpx.AsyncClient`` injetado. Se ``None``, cria um por chamada.
        Injete nos testes usando ``httpx.AsyncClient(transport=httpx.MockTransport(...))``.
    """

    def __init__(
        self,
        base_url: str = "https://api.elevenlabs.io/v1",
        token: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("ELEVENLABS_API_KEY", "")
        self._client = client

    @traced("adapter.elevenlabs.create_voice", run_type="tool", step=3, provider="elevenlabs")
    async def create_voice(self, index: int) -> str:
        """Cria voz de creator via POST ``{base_url}/voices/add``.

        Retorna o ``voice_id`` (string).
        """
        headers = {
            "xi-api-key": self.token,
            "Content-Type": "application/json",
        }
        body = {
            "name": f"creator-{index}",
        }

        if self._client is not None:
            resp = await self._client.post(
                f"{self.base_url}/voices/add",
                headers=headers,
                json=body,
            )
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/voices/add",
                    headers=headers,
                    json=body,
                )

        resp.raise_for_status()
        data = resp.json()
        return data["voice_id"]
