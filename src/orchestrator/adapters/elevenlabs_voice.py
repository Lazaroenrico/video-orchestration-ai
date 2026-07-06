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

from orchestrator.adapters._retry import with_transport_retry
from orchestrator.adapters.base import VoiceProfile
from orchestrator.tracing import traced

# Texto fixo curto para o preview de voz (~2s de áudio) — não precisa refletir o
# script real, só dar ao usuário uma amostra audível da voz do creator.
_PREVIEW_TEXT = "Oi! Essa é uma prévia da minha voz."


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
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("ELEVENLABS_API_KEY", "")
        self._client = client
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    @traced("adapter.elevenlabs.create_voice", run_type="tool", step=3, provider="elevenlabs")
    async def create_voice(
        self, index: int, voice_profile: Optional[VoiceProfile] = None
    ) -> str:
        """Cria voz de creator via POST ``{base_url}/voices/add``.

        Retorna o ``voice_id`` (string). Retenta em ``429`` (throttle transitório);
        demais erros HTTP propagam na hora.
        """
        headers = {
            "xi-api-key": self.token,
            "Content-Type": "application/json",
        }
        body = {
            "name": f"creator-{index}",
        }
        if voice_profile is not None:
            description = voice_profile.prompt or f"{voice_profile.preset} creator voice"
            body["description"] = description
            body["labels"] = {"preset": voice_profile.preset}

        async def _call() -> dict:
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
            return resp.json()

        data = await with_transport_retry(
            _call,
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="elevenlabs.create_voice",
        )
        return data["voice_id"]

    @traced(
        "adapter.elevenlabs.synthesize_preview", run_type="tool", step=3, provider="elevenlabs"
    )
    async def synthesize_preview(
        self, voice_id: str, text: str = _PREVIEW_TEXT
    ) -> bytes:
        """Sintetiza uma amostra curta (~2s) da voz via POST ``{base_url}/text-to-speech/{voice_id}``.

        Retorna os bytes de áudio (``audio/mpeg``) — usados para gerar o preview de
        voz do creator no dashboard (``creator_ready``/``voice_preview_uri``).
        """
        headers = {
            "xi-api-key": self.token,
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
        }

        if self._client is not None:
            resp = await self._client.post(
                f"{self.base_url}/text-to-speech/{voice_id}",
                headers=headers,
                json=body,
            )
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/text-to-speech/{voice_id}",
                    headers=headers,
                    json=body,
                )

        resp.raise_for_status()
        return resp.content
