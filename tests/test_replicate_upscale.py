"""Testes do ReplicateUpscaleAdapter — sem rede, usando httpx.MockTransport."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.adapters.replicate_upscale import ReplicateUpscaleAdapter

_FAKE_RESPONSE = {"id": "pred-upscale-1", "output": "https://cdn.replicate.com/upscaled.png"}
_captured_requests: list[httpx.Request] = []


def _mock_handler(request: httpx.Request) -> httpx.Response:
    _captured_requests.append(request)
    return httpx.Response(200, json=_FAKE_RESPONSE)


def _make_adapter(token: str = "test-token") -> ReplicateUpscaleAdapter:
    _captured_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    client = httpx.AsyncClient(transport=transport)
    return ReplicateUpscaleAdapter(
        base_url="https://api.replicate.com/v1",
        token=token,
        client=client,
    )


async def test_upscale_returns_url():
    adapter = _make_adapter()
    url = await adapter.upscale("https://example.com/image.png")
    assert url == "https://cdn.replicate.com/upscaled.png"


async def test_upscale_returns_string():
    adapter = _make_adapter()
    result = await adapter.upscale("https://example.com/img.jpg")
    assert isinstance(result, str)
    assert result.startswith("https://")


async def test_auth_header_sent():
    adapter = _make_adapter(token="my-secret-token")
    await adapter.upscale("https://example.com/img.png")
    assert len(_captured_requests) == 1
    auth = _captured_requests[0].headers.get("authorization", "")
    assert auth == "Token my-secret-token"


async def test_posts_to_predictions_endpoint():
    adapter = _make_adapter()
    await adapter.upscale("https://example.com/img.png")
    req = _captured_requests[0]
    assert req.method == "POST"
    assert str(req.url).endswith("/predictions")


async def test_request_body_contains_model_and_input():
    adapter = _make_adapter()
    await adapter.upscale("https://example.com/face.png")
    body: dict[str, Any] = json.loads(_captured_requests[0].content)
    assert body["model"] == "nightmareai/real-esrgan"
    assert body["input"]["image"] == "https://example.com/face.png"
    assert body["input"]["scale"] == 4


async def test_http_error_raises():
    def _error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    transport = httpx.MockTransport(_error_handler)
    client = httpx.AsyncClient(transport=transport)
    adapter = ReplicateUpscaleAdapter(client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.upscale("https://example.com/img.png")
