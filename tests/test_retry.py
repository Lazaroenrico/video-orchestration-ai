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


async def test_429_sleep_honors_reset_hint_from_detail(monkeypatch):
    """O 429 do Replicate diz quando o budget volta ("resets in ~8s"); o retry
    deve esperar pelo menos isso (com 1s de folga), não só o backoff exponencial."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("orchestrator.adapters._retry.asyncio.sleep", fake_sleep)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ReplicateError(
                status=429, detail="Request was throttled. resets in ~8s"
            )
        return "ok"

    result = await with_transport_retry(flaky, backoff_base=0)
    assert result == "ok"
    assert sleeps == [9.0]  # 8s do hint + 1s de folga > backoff 0


async def test_429_sleep_honors_expected_available_in_seconds(monkeypatch):
    """Variação de mensagem: "Expected available in 3 seconds"."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("orchestrator.adapters._retry.asyncio.sleep", fake_sleep)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ReplicateError(
                status=429,
                detail="Request was throttled. Expected available in 3 seconds.",
            )
        return "ok"

    result = await with_transport_retry(flaky, backoff_base=0)
    assert result == "ok"
    assert sleeps == [4.0]


async def test_429_without_hint_keeps_exponential_backoff(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("orchestrator.adapters._retry.asyncio.sleep", fake_sleep)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ReplicateError(status=429, detail="throttled")
        return "ok"

    result = await with_transport_retry(flaky, backoff_base=1.0)
    assert result == "ok"
    assert sleeps == [1.0, 2.0]  # backoff exponencial puro quando não há hint


async def test_429_hint_delay_is_capped_by_max_delay(monkeypatch):
    """O hint vem do corpo do 429 (externo, não confiável): um valor absurdo não
    pode fazer o pipeline dormir por horas — o delay é capado em max_delay."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("orchestrator.adapters._retry.asyncio.sleep", fake_sleep)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ReplicateError(
                status=429, detail="Request was throttled. resets in ~99999s"
            )
        return "ok"

    result = await with_transport_retry(flaky, backoff_base=0)
    assert result == "ok"
    assert sleeps == [60.0]  # capado no default de max_delay


async def test_exponential_backoff_is_capped_by_max_delay(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("orchestrator.adapters._retry.asyncio.sleep", fake_sleep)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 6:
            raise ReplicateError(status=429, detail="throttled")
        return "ok"

    result = await with_transport_retry(
        flaky, max_retries=5, backoff_base=16.0, max_delay=30.0
    )
    assert result == "ok"
    # 16, 32→30, 64→30, 128→30, 256→30: nenhum delay acima do cap
    assert sleeps == [16.0, 30.0, 30.0, 30.0, 30.0]


async def test_read_timeout_propagates_without_retry():
    """ReadTimeout é pós-envio: a prediction pode já ter iniciado no provedor.
    Retentar recriaria a prediction (dupla cobrança) — propaga na 1ª."""
    calls = 0

    async def slow_response():
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(httpx.ReadTimeout):
        await with_transport_retry(slow_response, backoff_base=0)
    assert calls == 1


async def test_connect_error_and_pool_timeout_still_retried():
    """Erros pré-envio (conexão nunca estabelecida / pool local) seguem retentáveis."""
    for exc in (httpx.ConnectError("refused"), httpx.PoolTimeout("pool exhausted")):
        calls = 0

        async def flaky(exc=exc):
            nonlocal calls
            calls += 1
            if calls < 2:
                raise exc
            return "ok"

        result = await with_transport_retry(flaky, backoff_base=0)
        assert result == "ok"
        assert calls == 2
