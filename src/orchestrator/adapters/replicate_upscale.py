"""ReplicateUpscaleAdapter — upscale de imagem via Replicate API.

Contrato HTTP (Replicate v1 simplificado):
POST ``{base_url}/predictions``

Request body::

    {
        "model": "nightmareai/real-esrgan",
        "input": {"image": "<url>", "scale": 4}
    }

Response JSON::

    {
        "id": "<prediction-id>",
        "output": "https://cdn.replicate.com/..."
    }

``output`` é uma string (não lista) — diferente do VideoAdapter que usa lista.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

_UPSCALE_MODEL = "nightmareai/real-esrgan"


class ReplicateUpscaleAdapter:
    """Faz upscale 4x de imagem via Replicate.

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

    async def upscale(self, image_url: str) -> str:
        """Faz upscale 4x da imagem. Retorna URL da imagem upscalada."""
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }
        body = {
            "model": _UPSCALE_MODEL,
            "input": {"image": image_url, "scale": 4},
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
        return data["output"]
