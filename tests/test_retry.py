"""Testes de ``with_transport_retry`` — retry de transporte + throttle 429.

Blips de conexão (``httpx.TransportError``) sempre foram retentados. O Replicate
throttla com ``429`` (``ReplicateError``) quando a conta tem crédito baixo (burst 1);
esse status é transitório ("resets in ~Ns") e também deve ser retentado. Outros
status HTTP (422/500) e erros de lógica propagam na hora.
"""
from __future__ import annotations

import httpx
import pytest
from replicate.exceptions import ReplicateError

from orchestrator.adapters._retry import with_transport_retry


async def test_retries_on_replicate_429_then_succeeds():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ReplicateError(status=429, detail="Request was throttled.")
        return "ok"

    result = await with_transport_retry(flaky, backoff_base=0)
    assert result == "ok"
    assert calls == 3  # 2 throttles + 1 sucesso


async def test_raises_after_exhausting_retries_on_persistent_429():
    calls = 0

    async def always_throttled():
        nonlocal calls
        calls += 1
        raise ReplicateError(status=429, detail="throttled")

    with pytest.raises(ReplicateError):
        await with_transport_retry(always_throttled, max_retries=2, backoff_base=0)
    assert calls == 3  # tentativa inicial + 2 retries


async def test_replicate_non_429_propagates_immediately():
    calls = 0

    async def unprocessable():
        nonlocal calls
        calls += 1
        raise ReplicateError(status=422, detail="bad input")

    with pytest.raises(ReplicateError):
        await with_transport_retry(unprocessable, backoff_base=0)
    assert calls == 1  # 422 não é retentável → propaga na 1ª


async def test_transport_error_still_retried():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise httpx.ConnectTimeout("connect failed")
        return "ok"

    result = await with_transport_retry(flaky, backoff_base=0)
    assert result == "ok"
    assert calls == 2


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.elevenlabs.io/v1/voices/add")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


async def test_retries_on_http_429_then_succeeds():
    """ElevenLabs (e outras APIs httpx puras) também throttlam com 429 via raise_for_status."""
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _http_status_error(429)
        return "ok"

    result = await with_transport_retry(flaky, backoff_base=0)
    assert result == "ok"
    assert calls == 3


async def test_http_non_429_status_error_propagates_immediately():
    calls = 0

    async def unauthorized():
        nonlocal calls
        calls += 1
        raise _http_status_error(401)

    with pytest.raises(httpx.HTTPStatusError):
        await with_transport_retry(unauthorized, backoff_base=0)
    assert calls == 1
