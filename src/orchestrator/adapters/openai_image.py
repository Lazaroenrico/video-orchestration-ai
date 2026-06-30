"""OpenAIImageAdapter — gera referência de rosto via GPT Image 2, implementa parcialmente CreatorPort.

## Contrato HTTP (OpenAI Images API, compatível com Vercel AI Gateway)
POST ``{base_url}/images/generations``

Headers::

    Authorization: Bearer <token>
    Content-Type: application/json

Request body::

    {
        "model": "openai/gpt-image-2",   # via Vercel Gateway (prefixo do provider)
        "prompt": "Professional creator face, front view, studio lighting, creator-{index}"
    }

Response JSON — duas formas suportadas:
- OpenAI direto (DALL·E): ``{"data": [{"url": "https://cdn.openai.com/..."}]}``
- GPT Image (e Vercel Gateway): ``{"data": [{"b64_json": "<base64 PNG>"}]}``

Quando vem ``b64_json``, a imagem é convertida num data URI
(``data:image/png;base64,...``) — isso permite que o upscaler downstream
(Replicate real-esrgan) aceite a imagem como ``input.image``.

``generate_face`` retorna os ângulos canônicos (front, 3/4, profile, smile,
neutral) junto com a imagem primária.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from orchestrator.tracing import traced

# Base OpenAI-compatível do Vercel AI Gateway (mesmo path do Chat Completions, sem /openai).
# Confirmado em https://vercel.com/docs/ai-gateway — image-only models usam /v1/images/generations.
_VERCEL_GATEWAY_OPENAI_BASE_URL = "https://ai-gateway.vercel.sh/v1"
_VERCEL_GATEWAY_IMAGE_MODEL = "openai/gpt-image-2"


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
        model: str = "gpt-image-2",
        timeout: float = 120.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.timeout = timeout
        self._client = client

    @traced("adapter.openai_image.generate_face", run_type="tool", step=3, provider="openai")
    async def generate_face(self, index: int, system_prompt: Optional[str] = None) -> dict[str, Any]:
        """Gera imagem de rosto via POST ``{base_url}/images/generations``.

        Retorna ``{"primary": <url ou data URI>, "angles": [...]}``.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        prompt = system_prompt if system_prompt else f"Professional creator face, front view, studio lighting, creator-{index}"
        body = {
            "model": self.model,
            "prompt": prompt,
        }

        if self._client is not None:
            resp = await self._client.post(
                f"{self.base_url}/images/generations",
                headers=headers,
                json=body,
            )
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/images/generations",
                    headers=headers,
                    json=body,
                )

        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        item: dict[str, Any] = data["data"][0]

        # OpenAI direto devolve uma URL; GPT Image / Vercel Gateway devolve base64.
        if item.get("url"):
            primary = item["url"]
        elif item.get("b64_json"):
            primary = f"data:image/png;base64,{item['b64_json']}"
        else:
            raise RuntimeError(
                "Image response contained neither 'url' nor 'b64_json'. "
                f"Keys present: {sorted(item)}"
            )

        return {
            "primary": primary,
            "angles": self.ANGLES,
        }


def build_openai_image_vercel_adapter(pipeline: dict[str, Any]) -> "OpenAIImageAdapter":
    """Cria OpenAIImageAdapter apontado para o Vercel AI Gateway.

    Usa o mesmo token ``AI_GATEWAY_API_KEY`` do LLMPort. O Gateway expõe os
    image-only models (GPT Image 2) no mesmo endpoint OpenAI-compatível
    ``{base}/images/generations`` — base ``https://ai-gateway.vercel.sh/v1``
    (NÃO ``/openai/v1``). O model precisa do prefixo do provider:
    ``openai/gpt-image-2``. Ambos podem ser sobrescritos por env:
    ``AI_GATEWAY_OPENAI_BASE_URL`` e ``AI_GATEWAY_OPENAI_MODEL``.
    """
    token = os.environ.get("AI_GATEWAY_API_KEY") or os.environ.get("VERCEL_OIDC_TOKEN")
    if not token:
        raise RuntimeError(
            "AI_GATEWAY_API_KEY ou VERCEL_OIDC_TOKEN é obrigatório para openai_image_vercel"
        )
    base_url = os.environ.get("AI_GATEWAY_OPENAI_BASE_URL", _VERCEL_GATEWAY_OPENAI_BASE_URL)
    model = os.environ.get("AI_GATEWAY_OPENAI_MODEL", _VERCEL_GATEWAY_IMAGE_MODEL)
    # GPT Image 2 pode levar 60-120 s (cold start); 180 s é mais seguro.
    # Sobrescrevível via AI_GATEWAY_IMAGE_TIMEOUT (segundos, float).
    timeout = float(os.environ.get("AI_GATEWAY_IMAGE_TIMEOUT", "180"))
    return OpenAIImageAdapter(base_url=base_url, token=token, model=model, timeout=timeout)
