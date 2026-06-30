"""ReplicateVideoAdapter — adapter de vídeo real estilo Replicate, implementa VideoPort.

## Decisão async vs httpx
``generate_clip`` é ``async`` por contrato (VideoPort). Para manter testabilidade sem
rede, optamos por ``httpx.AsyncClient`` (nativo async) em vez de rodar um
``httpx.Client`` sync em ``asyncio.to_thread``.

Vantagens da abordagem AsyncClient:
- Sem overhead de thread pool.
- Testável com ``httpx.MockTransport`` + ``httpx.AsyncClient`` direto, sem precisar de
  ``respx`` ou patches de thread.
- Composição natural com ``await``.

## Formato de resposta assumido (Replicate v1 simplificado)
POST ``{base_url}/predictions``

Request body::

    {
        "model": "<model-id>",
        "input": {"item_id": "...", "seconds": 8, "attempt": 1}
    }

Response JSON esperado::

    {
        "id": "<prediction-id>",
        "output": ["https://cdn.example.com/clip.mp4"]
    }

``output`` é uma lista; o URI do clip é o primeiro elemento.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from orchestrator.graph.state import Artifact
from orchestrator.tracing import traced


class ReplicateVideoAdapter:
    """Implementa VideoPort chamando a API Replicate (ou compatível).

    Parameters
    ----------
    tiers:
        Lista de dicts com ``name``, ``model``, ``cost_per_second`` (e opcionalmente
        ``max_concurrency``). Espelha o formato de ``conftest.TIERS``.
    base_url:
        Base da API. Padrão: ``https://api.replicate.com/v1``.
    token:
        Token de autenticação (``Authorization: Token <token>``).
        Se vazio, lê de ``REPLICATE_API_TOKEN``.
    client:
        ``httpx.AsyncClient`` injetado. Se ``None``, cria um por chamada.
        Injete nos testes usando ``httpx.AsyncClient(transport=httpx.MockTransport(...))``.
    """

    def __init__(
        self,
        tiers: list[dict[str, Any]],
        base_url: str = "https://api.replicate.com/v1",
        token: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.tiers: dict[str, dict[str, Any]] = {t["name"]: t for t in tiers}
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("REPLICATE_API_TOKEN", "")
        self._client = client

    @traced("adapter.replicate_video.generate_clip", run_type="tool", step="video", provider="replicate")
    async def generate_clip(
        self,
        item_id: str,
        tier: str,
        seconds: int,
        attempt: int,
        system_prompt: Optional[str] = None,
    ) -> Artifact:
        """Gera um clip via POST ``{base_url}/predictions``.

        Levanta ``KeyError`` para tier desconhecido (contratual, igual ao MockAdapter).
        """
        spec = self.tiers[tier]  # KeyError em tier desconhecido (contratual)
        model = spec["model"]
        cost_usd = round(spec["cost_per_second"] * seconds, 4)

        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }
        inp: dict[str, Any] = {
            "item_id": item_id,
            "seconds": seconds,
            "attempt": attempt,
        }
        if system_prompt:
            inp["prompt"] = system_prompt
        body = {
            "model": model,
            "input": inp,
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
        data: dict[str, Any] = resp.json()

        prediction_id: str = data["id"]
        uri: str = data["output"][0]

        return Artifact(
            kind="clip",
            uri=uri,
            meta={
                "tier": tier,
                "model": model,
                "seconds": seconds,
                "cost_usd": cost_usd,
                "attempt": attempt,
                "provider": "replicate",
                "prediction_id": prediction_id,
            },
        )
