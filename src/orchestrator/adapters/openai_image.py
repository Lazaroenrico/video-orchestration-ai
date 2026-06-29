"""OpenAIImageAdapter — gera referência de rosto via GPT Image 2, implementa parcialmente CreatorPort.

## Contrato HTTP assumido
POST ``{base_url}/images/generations``

Headers::

    Authorization: Bearer <token>
    Content-Type: application/json

Request body::

    {
        "model": "gpt-image-2",
        "prompt": "Professional creator face, front view, studio lighting, creator-{index}"
    }

Response JSON esperado::

    {
        "data": [{"url": "https://cdn.openai.com/..."}]
    }

O primeiro elemento de ``data`` é a imagem primária. ``generate_face`` retorna
os ângulos canônicos (front, 3/4, profile, smile, neutral) junto com a URL primária.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

_VERCEL_GATEWAY_OPENAI_BASE_URL = "https://ai-gateway.vercel.sh/openai/v1"


class OpenAIImageAdapter:
    """Gera referência de rosto de creator via GPT Image 2.

    Parameters
    ----------
    base_url:
        Base da API. Padrão: ``https://api.openai.com/v1``.
    token:
        Token de autenticação (``Authorization: Bearer <token>``).
        Se vazio, lê de ``OPENAI_API_KEY``.
    client:
        ``httpx.AsyncClient`` injetado. Se ``None``, cria um por chamada.
        Injete nos testes usando ``httpx.AsyncClient(transport=httpx.MockTransport(...))``.
    """

    ANGLES = ["front", "3/4", "profile", "smile", "neutral"]

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        token: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("OPENAI_API_KEY", "")
        self._client = client

    async def generate_face(self, index: int) -> dict[str, Any]:
        """Gera imagem de rosto via POST ``{base_url}/images/generations``.

        Retorna ``{"primary": <url>, "angles": [...]}``.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "gpt-image-2",
            "prompt": f"Professional creator face, front view, studio lighting, creator-{index}",
        }

        if self._client is not None:
            resp = await self._client.post(
                f"{self.base_url}/images/generations",
                headers=headers,
                json=body,
            )
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/images/generations",
                    headers=headers,
                    json=body,
                )

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        primary_url: str = data["data"][0]["url"]

        return {
            "primary": primary_url,
            "angles": self.ANGLES,
        }


def build_openai_image_vercel_adapter(pipeline: dict[str, Any]) -> "OpenAIImageAdapter":
    """Cria OpenAIImageAdapter apontado para o Vercel AI Gateway (path OpenAI).

    Usa o mesmo token ``AI_GATEWAY_API_KEY`` do LLMPort. A URL base é controlada
    por ``AI_GATEWAY_OPENAI_BASE_URL`` (separada de ``AI_GATEWAY_BASE_URL``, que
    é o path do Anthropic SDK e inclui ``/v1``).
    """
    token = os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("VERCEL_OIDC_TOKEN")
    if not token:
        raise RuntimeError(
            "AI_GATEWAY_API_KEY ou VERCEL_OIDC_TOKEN é obrigatório para openai_image_vercel"
        )
    base_url = os.environ.get("AI_GATEWAY_OPENAI_BASE_URL", _VERCEL_GATEWAY_OPENAI_BASE_URL)
    return OpenAIImageAdapter(base_url=base_url, token=token)
