"""AsyncThrottle — rate-limiter global das chamadas Replicate.

Contas Replicate com crédito baixo (<US$5) têm cap de ~6 req/min com burst 1; o
roster dispara upscale+voz+vídeo em paralelo e estoura o burst na hora (429).
O throttle serializa as chamadas (concurrency configurável, default 1) e impõe um
intervalo mínimo entre *inícios* de chamada, compartilhado por todos os adapters
Replicate do processo (voz, upscale e vídeo usam o MESMO orçamento).

Determinismo (CLAUDE.md): clock e sleep são injetáveis — nenhum teste dorme de verdade.
"""
from __future__ import annotations

import asyncio

import pytest

import orchestrator.adapters._throttle as throttle_mod
from orchestrator.adapters._throttle import AsyncThrottle, get_replicate_throttle
from orchestrator.adapters.replicate_upscale import ReplicateUpscaleAdapter
from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_sleep(clock: FakeClock, log: list[float]):
    async def _sleep(seconds: float) -> None:
        log.append(round(seconds, 6))
        clock.now += seconds

    return _sleep


async def test_spaces_call_starts_by_min_interval():
    clock = FakeClock()
    sleeps: list[float] = []
    throttle = AsyncThrottle(
        min_interval=10.0, concurrency=1, clock=clock, sleep=make_sleep(clock, sleeps)
    )
    starts: list[float] = []

    async def op(i: int):
        async def _fn():
            starts.append(clock.now)
            return i

        return await throttle.run(_fn)

    results = await asyncio.gather(op(0), op(1), op(2))
    assert results == [0, 1, 2]
    assert starts == [0.0, 10.0, 20.0]


async def test_zero_interval_does_not_sleep():
    clock = FakeClock()
    sleeps: list[float] = []
    throttle = AsyncThrottle(
        min_interval=0.0, concurrency=1, clock=clock, sleep=make_sleep(clock, sleeps)
    )

    async def _fn():
        return "ok"

    results = await asyncio.gather(*(throttle.run(_fn) for _ in range(3)))
    assert results == ["ok"] * 3
    assert sleeps == []


async def test_concurrency_one_never_overlaps_calls():
    throttle = AsyncThrottle(min_interval=0.0, concurrency=1)
    active = 0
    max_active = 0

    async def _fn():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)  # yield: daria chance de overlap sem o semáforo
        active -= 1
        return "ok"

    await asyncio.gather(*(throttle.run(_fn) for _ in range(5)))
    assert max_active == 1


async def test_interval_not_double_counted_when_calls_arrive_late():
    """Se a chamada seguinte chega DEPOIS do intervalo, não há espera extra."""
    clock = FakeClock()
    sleeps: list[float] = []
    throttle = AsyncThrottle(
        min_interval=10.0, concurrency=1, clock=clock, sleep=make_sleep(clock, sleeps)
    )

    async def _fn():
        return clock.now

    first = await throttle.run(_fn)
    clock.now = 25.0  # bem depois do próximo slot (10.0)
    second = await throttle.run(_fn)
    assert first == 0.0
    assert second == 25.0
    assert sleeps == []


async def test_exception_does_not_poison_throttle():
    clock = FakeClock()
    throttle = AsyncThrottle(
        min_interval=0.0, concurrency=1, clock=clock, sleep=make_sleep(clock, [])
    )

    async def boom():
        raise RuntimeError("falha da chamada")

    async def ok():
        return "ok"

    with pytest.raises(RuntimeError):
        await throttle.run(boom)
    assert await throttle.run(ok) == "ok"


def test_get_replicate_throttle_is_singleton_and_reads_env(monkeypatch):
    monkeypatch.setattr(throttle_mod, "_GLOBAL", None)
    monkeypatch.setenv("REPLICATE_MIN_INTERVAL_SECONDS", "7.5")
    monkeypatch.setenv("REPLICATE_MAX_CONCURRENCY", "2")
    throttle = get_replicate_throttle()
    assert throttle.min_interval == 7.5
    assert throttle.concurrency == 2
    assert get_replicate_throttle() is throttle


def test_get_replicate_throttle_defaults_to_low_credit_profile(monkeypatch):
    monkeypatch.setattr(throttle_mod, "_GLOBAL", None)
    monkeypatch.delenv("REPLICATE_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("REPLICATE_MAX_CONCURRENCY", raising=False)
    throttle = get_replicate_throttle()
    assert throttle.min_interval == 10.0
    assert throttle.concurrency == 1


@pytest.mark.parametrize("garbage", ["8x", "", "auto"])
def test_get_replicate_throttle_falls_back_on_malformed_env(monkeypatch, caplog, garbage):
    """Um typo na env não pode derrubar a primeira chamada Replicate do pipeline:
    valor não numérico cai nos defaults, com warning."""
    monkeypatch.setattr(throttle_mod, "_GLOBAL", None)
    monkeypatch.setenv("REPLICATE_MIN_INTERVAL_SECONDS", garbage)
    monkeypatch.setenv("REPLICATE_MAX_CONCURRENCY", garbage)

    with caplog.at_level("WARNING"):
        throttle = get_replicate_throttle()

    assert throttle.min_interval == 10.0
    assert throttle.concurrency == 1
    assert any("REPLICATE_MIN_INTERVAL_SECONDS" in r.message for r in caplog.records)
    assert any("REPLICATE_MAX_CONCURRENCY" in r.message for r in caplog.records)


class SpyThrottle:
    """Registra que a chamada passou pelo throttle antes de executar."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, fn):
        self.calls += 1
        return await fn()


async def test_voice_adapter_routes_runner_through_throttle(monkeypatch):
    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL", "elevenlabs/turbo-v2.5")

    async def runner(ref, input):
        return "https://replicate.delivery/voice.mp3"

    spy = SpyThrottle()
    adapter = ReplicateVoiceAdapter(runner=runner, throttle=spy)
    result = await adapter.create_voice(0)
    assert result == "https://replicate.delivery/voice.mp3"
    assert spy.calls == 1


async def test_upscale_adapter_routes_runner_through_throttle():
    async def runner(ref, input):
        return "https://replicate.delivery/up.png"

    spy = SpyThrottle()
    adapter = ReplicateUpscaleAdapter(runner=runner, throttle=spy)
    result = await adapter.upscale("https://img.example/x.png")
    assert result == "https://replicate.delivery/up.png"
    assert spy.calls == 1


async def test_video_adapter_routes_runner_through_throttle(pipeline_cfg):
    async def runner(ref, input):
        return "https://replicate.delivery/clip.mp4"

    spy = SpyThrottle()
    adapter = ReplicateVideoAdapter(
        tiers=pipeline_cfg["tiers"], runner=runner, throttle=spy
    )
    art = await adapter.generate_clip(item_id="item-1", tier="ltx", seconds=8, attempt=0)
    assert art.uri == "https://replicate.delivery/clip.mp4"
    assert spy.calls == 1


async def test_throttle_applies_to_each_retry_attempt(monkeypatch):
    """Cada tentativa (inclusive retries de 429) reentra no throttle."""
    from replicate.exceptions import ReplicateError

    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL", "elevenlabs/turbo-v2.5")
    calls = 0

    async def flaky_runner(ref, input):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ReplicateError(status=429, detail="throttled")
        return "https://replicate.delivery/voice.mp3"

    spy = SpyThrottle()
    adapter = ReplicateVoiceAdapter(runner=flaky_runner, throttle=spy, backoff_base=0)
    result = await adapter.create_voice(0)
    assert result == "https://replicate.delivery/voice.mp3"
    assert spy.calls == 2


def test_replicate_factories_share_the_global_throttle(monkeypatch, pipeline_cfg):
    """Voz e vídeo dividem o MESMO orçamento de rate limit do processo.

    (O upscale de imagem saiu do creator — o upscale agora é do vídeo final e não usa
    Replicate no perfil live; ver papel ``upscale``/``passthrough_upscale``.)
    """
    from orchestrator.adapters.creator_real import build_real_creator_replicate_adapter
    from orchestrator.registry import _build_replicate

    monkeypatch.setattr(throttle_mod, "_GLOBAL", None)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL", "elevenlabs/turbo-v2.5")

    creator = build_real_creator_replicate_adapter(pipeline_cfg)
    video = _build_replicate(pipeline_cfg)

    shared = get_replicate_throttle()
    assert creator.voice._throttle is shared
    assert video._throttle is shared
