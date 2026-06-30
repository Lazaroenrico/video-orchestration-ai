"""Testes do ReplicateVoiceAdapter — sem rede, usando httpx.MockTransport."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter

_FAKE_RESPONSE = {"id": "pred-voice-42", "status": "starting", "output": None}
_captured_requests: list[httpx.Request] = []


def _mock_handler(request: httpx.Request) -> httpx.Response:
    _captured_requests.append(request)
    return httpx.Response(200, json=_FAKE_RESPONSE)


def _make_adapter(token: str = "test-token") -> ReplicateVoiceAdapter:
    _captured_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    client = httpx.AsyncClient(transport=transport)
    return ReplicateVoiceAdapter(
        base_url="https://api.replicate.com/v1",
        token=token,
        client=client,
    )


async def test_create_voice_returns_string():
    adapter = _make_adapter()
    voice_id = await adapter.create_voice(0)
    assert isinstance(voice_id, str)
    assert len(voice_id) > 0


async def test_create_voice_returns_prediction_id():
    adapter = _make_adapter()
    voice_id = await adapter.create_voice(1)
    assert voice_id == "pred-voice-42"


async def test_auth_header_sent():
    adapter = _make_adapter(token="replicate-secret")
    await adapter.create_voice(0)
    assert len(_captured_requests) == 1
    auth = _captured_requests[0].headers.get("authorization", "")
    assert auth == "Token replicate-secret"


async def test_posts_to_predictions_endpoint():
    adapter = _make_adapter()
    await adapter.create_voice(0)
    req = _captured_requests[0]
    assert req.method == "POST"
    assert str(req.url).endswith("/predictions")


async def test_request_body_contains_model_and_input():
    adapter = _make_adapter()
    await adapter.create_voice(3)
    body: dict[str, Any] = json.loads(_captured_requests[0].content)
    assert body["model"] == "suno-ai/bark"
    assert "3" in body["input"]["prompt"]


async def test_different_indices_produce_different_prompts():
    reqs: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        reqs.append(request)
        return httpx.Response(200, json=_FAKE_RESPONSE)

    transport = httpx.MockTransport(_capture)
    client = httpx.AsyncClient(transport=transport)
    adapter = ReplicateVoiceAdapter(client=client)
    await adapter.create_voice(0)
    await adapter.create_voice(5)
    body0 = json.loads(reqs[0].content)
    body5 = json.loads(reqs[1].content)
    assert body0["input"]["prompt"] != body5["input"]["prompt"]


async def test_http_error_raises():
    def _error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "invalid input"})

    transport = httpx.MockTransport(_error_handler)
    client = httpx.AsyncClient(transport=transport)
    adapter = ReplicateVoiceAdapter(client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.create_voice(0)
