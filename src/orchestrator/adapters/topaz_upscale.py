"""TopazUpscaleAdapter — upscale de imagem via Topaz Labs API.

## Contrato HTTP assumido
POST ``{base_url}/upscale``

Headers::

    Authorization: Bearer <token>
    Content-Type: application/json

Request body::

    {
        "image_url": "<url da imagem primária>",
        "scale": 4
    }

Response JSON esperado::

    {
        "output_url": "https://cdn.topazlabs.com/..."
    }

``upscale`` retorna a string ``output_url`` diretamente.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

from orchestrator.adapters._retry import with_transport_retry
from orchestrator.tracing import traced


class TopazUpscaleAdapter:
    """Upscale de referência primária do creator via Topaz Labs.

    Parameters
    ----------
    base_url:
        Base da API. Padrão: ``https://api.topazlabs.com/v1``.
    token:
        Token de autenticação (``Authorization: Bearer <token>``).
        Se vazio, lê de ``TOPAZ_API_KEY``.
    client:
        ``httpx.AsyncClient`` injetado. Se ``None``, cria um por chamada.
        Injete nos testes usando ``httpx.AsyncClient(transport=httpx.MockTransport(...))``.
    """

    def __init__(
        self,
        base_url: str = "https://api.topazlabs.com/v1",
        token: str = "",
        client: Optional[httpx.AsyncClient] = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("TOPAZ_API_KEY", "")
        self._client = client
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    @traced("adapter.topaz.upscale", run_type="tool", step=3, provider="topaz")
    async def upscale(self, image_url: str) -> str:
        """Faz upscale 4x via POST ``{base_url}/upscale``.

        Retorna a URL da imagem upscalada (string). Retenta em ``429`` (throttle
        transitório); demais erros HTTP propagam na hora.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        body = {
            "image_url": image_url,
            "scale": 4,
        }

        async def _call() -> dict:
            if self._client is not None:
                resp = await self._client.post(
                    f"{self.base_url}/upscale",
                    headers=headers,
                    json=body,
                )
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{self.base_url}/upscale",
                        headers=headers,
                        json=body,
                    )
            resp.raise_for_status()
            return resp.json()

        data = await with_transport_retry(
            _call,
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="topaz.upscale",
        )
        return data["output_url"]
