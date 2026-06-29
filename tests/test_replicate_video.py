"""Testes do ReplicateVideoAdapter — sem rede, usando httpx.MockTransport."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.graph.state import Artifact

# Tiers idênticos aos do conftest.py
TIERS = [
    {"name": "ltx",      "model": "ltx-2.3",      "cost_per_second": 0.01,  "max_concurrency": 16},
    {"name": "kling",    "model": "kling-3.0",     "cost_per_second": 0.10,  "max_concurrency": 6},
    {"name": "seedance", "model": "seedance-2.0",  "cost_per_second": 0.168, "max_concurrency": 2},
]

# Resposta fictícia estilo Replicate
_FAKE_RESPONSE = {"id": "pred-1", "output": ["https://cdn/clip.mp4"]}

# Captura para inspecionar requests
_captured_requests: list[httpx.Request] = []


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Handler do MockTransport: captura o request e devolve a resposta fake."""
    _captured_requests.append(request)
    return httpx.Response(200, json=_FAKE_RESPONSE)


def _make_adapter(token: str = "test-token") -> ReplicateVideoAdapter:
    """Cria um adapter com client mockado e limpa a lista de requests capturados."""
    _captured_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    client = httpx.AsyncClient(transport=transport)
    return ReplicateVideoAdapter(
        tiers=TIERS,
        base_url="https://api.replicate.com/v1",
        token=token,
        client=client,
    )


# ---------------------------------------------------------------------------
# Testes de retorno de Artifact
# ---------------------------------------------------------------------------

async def test_generate_clip_returns_artifact():
    """generate_clip deve retornar um Artifact com kind='clip'."""
    adapter = _make_adapter()
    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)
    assert isinstance(artifact, Artifact)
    assert artifact.kind == "clip"
    assert artifact.uri == "https://cdn/clip.mp4"


async def test_generate_clip_meta_provider_and_prediction_id():
    """meta deve conter provider='replicate' e prediction_id correto."""
    adapter = _make_adapter()
    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)
    assert artifact.meta["provider"] == "replicate"
    assert artifact.meta["prediction_id"] == "pred-1"


async def test_generate_clip_meta_tier_model_seconds_attempt():
    """meta deve conter tier, model, seconds e attempt."""
    adapter = _make_adapter()
    artifact = await adapter.generate_clip("item-xyz", "kling", 10, 2)
    assert artifact.meta["tier"] == "kling"
    assert artifact.meta["model"] == "kling-3.0"
    assert artifact.meta["seconds"] == 10
    assert artifact.meta["attempt"] == 2


# ---------------------------------------------------------------------------
# Testes de cost_usd por tier
# ---------------------------------------------------------------------------

async def test_cost_ltx():
    """LTX: $0.01/s × 8s = $0.08."""
    adapter = _make_adapter()
    artifact = await adapter.generate_clip("item-1", "ltx", 8, 1)
    assert artifact.meta["cost_usd"] == pytest.approx(0.08, abs=1e-6)


async def test_cost_kling():
    """Kling: $0.10/s × 8s = $0.80."""
    adapter = _make_adapter()
    artifact = await adapter.generate_clip("item-1", "kling", 8, 1)
    assert artifact.meta["cost_usd"] == pytest.approx(0.80, abs=1e-6)


async def test_cost_seedance():
    """Seedance: $0.168/s × 8s = $1.344."""
    adapter = _make_adapter()
    artifact = await adapter.generate_clip("item-1", "seedance", 8, 1)
    assert artifact.meta["cost_usd"] == pytest.approx(1.344, abs=1e-6)


async def test_cost_rounded_to_4_decimals():
    """cost_usd deve ter no máximo 4 casas decimais (round)."""
    adapter = _make_adapter()
    artifact = await adapter.generate_clip("item-1", "seedance", 7, 1)
    # 0.168 * 7 = 1.176 (exato aqui, mas garante o round)
    assert artifact.meta["cost_usd"] == round(0.168 * 7, 4)


# ---------------------------------------------------------------------------
# Teste de tier desconhecido
# ---------------------------------------------------------------------------

async def test_unknown_tier_raises_key_error():
    """Tier inexistente deve levantar KeyError (contratual com MockAdapter)."""
    adapter = _make_adapter()
    with pytest.raises(KeyError):
        await adapter.generate_clip("item-1", "nonexistent_tier", 8, 1)


# ---------------------------------------------------------------------------
# Teste de header de autenticação
# ---------------------------------------------------------------------------

async def test_auth_header_sent():
    """O header Authorization deve ser enviado como 'Token <token>'."""
    adapter = _make_adapter(token="my-secret-token")
    await adapter.generate_clip("item-1", "ltx", 8, 1)
    assert len(_captured_requests) == 1
    auth = _captured_requests[0].headers.get("authorization", "")
    assert auth == "Token my-secret-token"


async def test_posts_to_predictions_endpoint():
    """A requisição deve ser feita para /predictions via POST."""
    adapter = _make_adapter()
    await adapter.generate_clip("item-1", "ltx", 8, 1)
    req = _captured_requests[0]
    assert req.method == "POST"
    assert str(req.url).endswith("/predictions")


async def test_request_body_contains_model_and_input():
    """O body deve ter 'model' e 'input' com item_id, seconds e attempt."""
    adapter = _make_adapter()
    await adapter.generate_clip("item-99", "kling", 12, 3)
    req = _captured_requests[0]
    body: dict[str, Any] = json.loads(req.content)
    assert body["model"] == "kling-3.0"
    assert body["input"]["item_id"] == "item-99"
    assert body["input"]["seconds"] == 12
    assert body["input"]["attempt"] == 3
