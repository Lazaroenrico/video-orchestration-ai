"""tests/test_tracing.py — TDD para o módulo orchestrator.tracing.

Todos os testes rodam offline (LANGSMITH_TRACING off / lib no-op):
- Transparência: decorated vs undecorated retorna saída idêntica.
- run_trace_config: devolve run_name/tags/metadata corretos.
- add_trace_metadata: no-op sem run ativo, não lança exceção.
- Import-safe: funciona mesmo que langsmith não esteja instalado.
- wrap_anthropic_client: devolve o próprio objeto quando tracing off/indisponível.
- isinstance check: Protocols runtime_checkable batem depois de decorar.
- Métodos decorados com @traced mantêm natureza async (são coroutines).
"""
from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, Optional
import unittest.mock as mock

import pytest

# Garante que tracing está desligado (no-op) para todos esses testes
os.environ.setdefault("LANGSMITH_TRACING", "false")


# --------------------------------------------------------------------------- #
# Import-safe                                                                  #
# --------------------------------------------------------------------------- #

def test_import_safe():
    """Módulo tracing importa sem erro mesmo que langsmith não esteja disponível."""
    from orchestrator.tracing import (  # noqa: F401
        traced, wrap_anthropic_client, add_trace_metadata, run_trace_config,
    )


def test_is_tracing_enabled_reads_env_at_runtime(monkeypatch):
    """Gate do tracing deve seguir LANGSMITH_TRACING em runtime."""
    from orchestrator.tracing import is_tracing_enabled

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    assert is_tracing_enabled() is False

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert is_tracing_enabled() is True


def test_traceable_marker_present_when_tracing_off(monkeypatch):
    """Mesmo em passthrough offline, @traced expõe marcador testável."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    from orchestrator.tracing import traced

    @traced("custom.span", run_type="tool", step=4)
    async def sample() -> str:
        return "ok"

    assert sample.__trace_name__ == "custom.span"
    assert sample.__trace_run_type__ == "tool"
    assert sample.__trace_metadata__ == {"step": 4}


def test_traced_does_not_call_langsmith_when_env_off(monkeypatch):
    """Com langsmith instalado mas tracing off, @traced não chama LangSmith."""
    import orchestrator.tracing as tracing_mod

    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    async def fn() -> str:
        return "ok"

    with mock.patch.object(tracing_mod, "_ls_traceable") as traceable_mock:
        wrapped = tracing_mod.traced("off.span")(fn)

    traceable_mock.assert_not_called()
    assert wrapped.__trace_name__ == "off.span"


def test_wrap_anthropic_client_returns_same_object_when_tracing_off(monkeypatch):
    """Com langsmith disponível mas tracing=false, client Anthropic não deve ser wrapped."""
    import orchestrator.tracing as tracing_mod

    class DummyClient:
        pass

    dummy = DummyClient()
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    with mock.patch.object(tracing_mod, "_HAS_LS", True):
        result = tracing_mod.wrap_anthropic_client(dummy)

    assert result is dummy


def test_drop_self_removes_config_and_headers_like_values():
    """Sanitizer evita serializar self/config/clients/headers/tokens em spans."""
    from orchestrator.tracing import _drop_sensitive_inputs

    result = _drop_sensitive_inputs({
        "self": object(),
        "config": {"configurable": {"adapter": object()}},
        "client": object(),
        "headers": {"Authorization": "Bearer secret"},
        "token": "secret",
        "item_id": "abc",
    })

    assert result == {"item_id": "abc"}


def test_sanitizer_keeps_prompts_visible_by_default(monkeypatch):
    """Por padrão, prompts/scripts aparecem no trace (debug); só base64 é elidido."""
    monkeypatch.delenv("LANGSMITH_REDACT_PROMPTS", raising=False)
    from orchestrator.tracing import _sanitize_trace_payload

    payload = {
        "offer": "secret product",
        "system_prompt": "make a face",
        "image_url": "data:image/png;base64," + ("a" * 300),  # base64 sempre elidido
        "output": {"script": "HOOK: private script", "item_id": "item-1"},
        "tier": "ltx",
        "attempt": 1,
    }

    assert _sanitize_trace_payload(payload) == {
        "offer": "secret product",
        "system_prompt": "make a face",
        "image_url": "<base64 elided>",
        "output": {"script": "HOOK: private script", "item_id": "item-1"},
        "tier": "ltx",
        "attempt": 1,
    }


def test_sanitizer_redacts_prompts_when_flag_enabled(monkeypatch):
    """Com LANGSMITH_REDACT_PROMPTS on, conteúdo é redigido mas ids/tier permanecem."""
    monkeypatch.setenv("LANGSMITH_REDACT_PROMPTS", "1")
    from orchestrator.tracing import _sanitize_trace_payload

    payload = {
        "offer": "secret product",
        "system_prompt": "make a face",
        "output": {"script": "HOOK: private script", "item_id": "item-1"},
        "tier": "ltx",
    }

    assert _sanitize_trace_payload(payload) == {
        "offer": "<redacted>",
        "system_prompt": "<redacted>",
        "output": {"script": "<redacted>", "item_id": "item-1"},
        "tier": "ltx",
    }


def test_sanitizer_always_drops_secrets_regardless_of_flag(monkeypatch):
    """Segredos são removidos com ou sem a flag de redação de prompt."""
    from orchestrator.tracing import _sanitize_trace_payload

    payload = {"api_key": "sk-secret", "token": "abc", "item_id": "item-1"}
    for flag in ("", "1"):
        monkeypatch.setenv("LANGSMITH_REDACT_PROMPTS", flag)
        assert _sanitize_trace_payload(payload) == {"item_id": "item-1"}


@pytest.mark.asyncio
async def test_traced_processes_inputs_and_outputs(monkeypatch):
    """Com tracing on e redação off (default), prompts são visíveis mas segredos caem."""
    import orchestrator.tracing as tracing_mod

    captured = {}

    def fake_traceable(**kwargs):
        captured["kwargs"] = kwargs

        def deco(fn):
            async def wrapped(*args, **call_kwargs):
                raw_inputs = {"args": args, **call_kwargs}
                captured["inputs"] = kwargs["process_inputs"](raw_inputs)
                out = await fn(*args, **call_kwargs)
                captured["outputs"] = kwargs["process_outputs"](out)
                return out

            return wrapped

        return deco

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_REDACT_PROMPTS", raising=False)
    monkeypatch.setattr(tracing_mod, "_HAS_LS", True)
    monkeypatch.setattr(tracing_mod, "_ls_traceable", fake_traceable)

    @tracing_mod.traced("secure.span")
    async def sample(system_prompt: str, token: str) -> dict:
        return {"script": "private script", "item_id": "item-1"}

    result = await sample(system_prompt="private prompt", token="sk-secret")

    assert result["script"] == "private script"
    # prompt visível para debug; segredo (token) sempre removido do span
    assert captured["inputs"]["system_prompt"] == "private prompt"
    assert "token" not in captured["inputs"]
    assert captured["outputs"] == {"script": "private script", "item_id": "item-1"}
    assert captured["kwargs"]["process_outputs"] is not None


# --------------------------------------------------------------------------- #
# run_trace_config                                                              #
# --------------------------------------------------------------------------- #

def test_run_trace_config_basic():
    from orchestrator.tracing import run_trace_config
    result = run_trace_config("run-abc123", offer="serum X", platform="tiktok", batch=12)
    assert result["run_name"] == "ugc-run:run-abc123"
    assert "tiktok" in result["tags"]
    assert not any("serum X" in t for t in result["tags"])
    assert any(t.startswith("offer_hash:") for t in result["tags"])
    assert "batch:12" in result["tags"]
    assert result["metadata"]["run_id"] == "run-abc123"
    assert result["metadata"]["offer_hash"]
    assert "offer" not in result["metadata"]
    assert result["metadata"]["platform"] == "tiktok"


def test_run_trace_config_no_offer_no_batch():
    from orchestrator.tracing import run_trace_config
    result = run_trace_config("run-xyz", platform="instagram")
    assert result["run_name"] == "ugc-run:run-xyz"
    assert "instagram" in result["tags"]
    # sem offer e sem batch, as tags correspondentes não aparecem
    assert not any(t.startswith("offer:") for t in result["tags"])
    assert not any(t.startswith("batch:") for t in result["tags"])


def test_run_trace_config_no_platform():
    from orchestrator.tracing import run_trace_config
    result = run_trace_config("run-noplat", offer="prod Y")
    # platform=None: não deve aparecer None na lista de tags
    assert None not in result["tags"]
    assert not any("prod Y" in t for t in result["tags"])
    assert any(t.startswith("offer_hash:") for t in result["tags"])


# --------------------------------------------------------------------------- #
# add_trace_metadata — no-op sem run ativo                                     #
# --------------------------------------------------------------------------- #

def test_add_trace_metadata_noop():
    from orchestrator.tracing import add_trace_metadata
    # Não deve lançar exceção mesmo sem run ativo
    add_trace_metadata(qc_score=0.95, qc_passed=True, attempt=1)


# --------------------------------------------------------------------------- #
# wrap_anthropic_client — passthrough offline                                  #
# --------------------------------------------------------------------------- #

def test_wrap_anthropic_client_returns_same_object_when_lib_unavailable():
    """Quando langsmith NÃO está disponível, wrap_anthropic_client devolve o próprio objeto."""
    import unittest.mock as mock
    import orchestrator.tracing as tracing_mod

    class DummyClient:
        pass

    dummy = DummyClient()

    # Simula ambiente sem langsmith (_HAS_LS = False)
    with mock.patch.object(tracing_mod, "_HAS_LS", False):
        result = tracing_mod.wrap_anthropic_client(dummy)

    assert result is dummy, "Deve retornar o mesmo objeto quando lib unavailable"


# --------------------------------------------------------------------------- #
# _sanitize_string — truncamento de strings gigantes                          #
# --------------------------------------------------------------------------- #

def test_sanitize_string_truncates_huge_strings():
    from orchestrator.tracing import _MAX_TRACE_STRING, _sanitize_string

    long = "a" * (_MAX_TRACE_STRING + 500)
    out = _sanitize_string(long)

    assert out.endswith("…[truncated]")
    assert out.startswith("a" * 10)
    assert len(out) == _MAX_TRACE_STRING + len("…[truncated]")


# --------------------------------------------------------------------------- #
# Ramos ativos (tracing on): wrapper síncrono, wrap_anthropic, run tree        #
# --------------------------------------------------------------------------- #

def test_traced_sync_wrapper_calls_langsmith_when_on(monkeypatch):
    """Função síncrona decorada com @traced usa o wrapper LangSmith quando tracing on."""
    import orchestrator.tracing as tracing_mod

    captured: dict[str, bool] = {}

    def fake_traceable(**kwargs):
        def deco(fn):
            def wrapped(*a, **k):
                captured["called"] = True
                return fn(*a, **k)

            return wrapped

        return deco

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(tracing_mod, "_HAS_LS", True)
    monkeypatch.setattr(tracing_mod, "_ls_traceable", fake_traceable)

    @tracing_mod.traced("sync.span")
    def sample(x: int) -> int:
        return x * 2

    assert sample(3) == 6
    assert captured.get("called") is True


def test_traced_sync_wrapper_passthrough_when_off(monkeypatch):
    """Função síncrona decorada é passthrough puro quando tracing off."""
    import orchestrator.tracing as tracing_mod

    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    @tracing_mod.traced("sync.off.span")
    def sample(x: int) -> int:
        return x + 1

    assert sample(41) == 42
    assert sample.__trace_name__ == "sync.off.span"


def test_wrap_anthropic_client_wraps_when_tracing_on(monkeypatch):
    import orchestrator.tracing as tracing_mod

    sentinel = object()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(tracing_mod, "_HAS_LS", True)
    monkeypatch.setattr(tracing_mod, "_ls_wrap_anthropic", lambda c: sentinel)

    assert tracing_mod.wrap_anthropic_client(object()) is sentinel


def test_add_trace_metadata_updates_active_run_tree(monkeypatch):
    import orchestrator.tracing as tracing_mod

    class _RT:
        def __init__(self) -> None:
            self.metadata: dict = {}

    rt = _RT()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_REDACT_PROMPTS", raising=False)
    monkeypatch.setattr(tracing_mod, "_HAS_LS", True)
    monkeypatch.setattr(tracing_mod, "get_current_run_tree", lambda: rt)

    tracing_mod.add_trace_metadata(step=3, offer="visible-by-default")

    assert rt.metadata["step"] == 3
    assert rt.metadata["offer"] == "visible-by-default"


def test_add_trace_metadata_swallows_run_tree_errors(monkeypatch):
    """Erro ao acessar a run tree nunca propaga — é chamado no meio de API real."""
    import orchestrator.tracing as tracing_mod

    def boom():
        raise RuntimeError("sem run tree")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(tracing_mod, "_HAS_LS", True)
    monkeypatch.setattr(tracing_mod, "get_current_run_tree", boom)

    tracing_mod.add_trace_metadata(step=1)  # não deve levantar


def test_wrap_anthropic_client_does_not_raise():
    """wrap_anthropic_client não lança exceção com qualquer objeto quando _HAS_LS=False."""
    import unittest.mock as mock
    import orchestrator.tracing as tracing_mod

    with mock.patch.object(tracing_mod, "_HAS_LS", False):
        result = tracing_mod.wrap_anthropic_client(object())
    assert result is not None


# --------------------------------------------------------------------------- #
# @traced — transparência offline (saída IDÊNTICA)                             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_traced_preserves_async_return_value():
    """Método async decorado com @traced retorna o mesmo valor offline."""
    from orchestrator.tracing import traced

    class MyAdapter:
        @traced(run_type="chain")
        async def compute(self, x: int) -> int:
            return x * 2

    adapter = MyAdapter()
    result = await adapter.compute(21)
    assert result == 42


def test_traced_async_is_coroutine():
    """@traced preserva natureza async — método decorado continua sendo coroutine."""
    from orchestrator.tracing import traced

    class MyAdapter:
        @traced(run_type="tool")
        async def do_something(self) -> str:
            return "ok"

    adapter = MyAdapter()
    # método deve ser coroutine function
    assert inspect.iscoroutinefunction(adapter.do_something)


@pytest.mark.asyncio
async def test_traced_output_identical_to_undecorated():
    """Saída de método decorado é byte-idêntica à versão sem decorator offline."""
    from orchestrator.tracing import traced

    class Plain:
        async def compute(self, data: str) -> str:
            return f"result:{data}"

    class Decorated:
        @traced(run_type="chain")
        async def compute(self, data: str) -> str:
            return f"result:{data}"

    plain = Plain()
    dec = Decorated()
    assert await plain.compute("abc") == await dec.compute("abc")


@pytest.mark.asyncio
async def test_traced_exception_propagates():
    """@traced não engole exceções."""
    from orchestrator.tracing import traced

    class MyAdapter:
        @traced(run_type="tool")
        async def fail(self) -> None:
            raise ValueError("boom")

    adapter = MyAdapter()
    with pytest.raises(ValueError, match="boom"):
        await adapter.fail()


# --------------------------------------------------------------------------- #
# @traced com MockAdapter — saída idêntica (determinismo intacto)              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_mock_adapter_build_creator_identical_after_tracing():
    """Decorar build_creator do MockAdapter não altera saída determinística."""
    from orchestrator.adapters.mock import MockAdapter
    from orchestrator.tracing import traced

    tiers = [{"name": "ltx", "model": "ltx-video", "cost_per_second": 0.01, "max_concurrency": 4}]

    # Versão sem decorator
    plain = MockAdapter(tiers=tiers)

    # Versão com decorator aplicado dinamicamente (simula o que o código faria)
    class DecoratedMock(MockAdapter):
        @traced(run_type="chain")
        async def build_creator(self, index: int, system_prompt: Optional[str] = None) -> dict[str, Any]:
            return await super().build_creator(index=index, system_prompt=system_prompt)

    dec = DecoratedMock(tiers=tiers)

    for i in range(3):
        plain_out = await plain.build_creator(i)
        dec_out = await dec.build_creator(i)
        assert plain_out == dec_out, f"Mismatch at index={i}: {plain_out} != {dec_out}"


@pytest.mark.asyncio
async def test_mock_adapter_generate_clip_identical_after_tracing():
    """Decorar generate_clip do MockAdapter não altera Artifacts determinísticos."""
    from orchestrator.adapters.mock import MockAdapter
    from orchestrator.tracing import traced

    tiers = [{"name": "ltx", "model": "ltx-video", "cost_per_second": 0.01, "max_concurrency": 4}]

    plain = MockAdapter(tiers=tiers)

    class DecoratedMock(MockAdapter):
        @traced(run_type="tool")
        async def generate_clip(self, item_id: str, tier: str, seconds: int, attempt: int,
                                system_prompt: Optional[str] = None):
            return await super().generate_clip(item_id=item_id, tier=tier, seconds=seconds,
                                               attempt=attempt, system_prompt=system_prompt)

    dec = DecoratedMock(tiers=tiers)

    plain_art = await plain.generate_clip("item-1", "ltx", 8, 0)
    dec_art = await dec.generate_clip("item-1", "ltx", 8, 0)
    assert plain_art.uri == dec_art.uri
    assert plain_art.meta == dec_art.meta


# --------------------------------------------------------------------------- #
# Protocol instanceof check após decorar                                        #
# --------------------------------------------------------------------------- #

def test_protocol_isinstance_after_tracing_creator_port():
    """Decorar build_creator não quebra isinstance(adapter, CreatorPort)."""
    from orchestrator.adapters.base import CreatorPort
    from orchestrator.adapters.mock import MockAdapter
    from orchestrator.tracing import traced

    tiers = [{"name": "ltx", "model": "ltx-video", "cost_per_second": 0.01, "max_concurrency": 4}]

    class DecoratedMock(MockAdapter):
        @traced(run_type="chain")
        async def build_creator(self, index: int, system_prompt: Optional[str] = None) -> dict[str, Any]:
            return await super().build_creator(index=index, system_prompt=system_prompt)

    dec = DecoratedMock(tiers=tiers)
    assert isinstance(dec, CreatorPort), "isinstance(dec, CreatorPort) falhou após @traced"


def test_protocol_isinstance_after_tracing_video_port():
    """Decorar generate_clip não quebra isinstance(adapter, VideoPort)."""
    from orchestrator.adapters.base import VideoPort
    from orchestrator.adapters.mock import MockAdapter
    from orchestrator.tracing import traced

    tiers = [{"name": "ltx", "model": "ltx-video", "cost_per_second": 0.01, "max_concurrency": 4}]

    class DecoratedMock(MockAdapter):
        @traced(run_type="tool")
        async def generate_clip(self, item_id: str, tier: str, seconds: int, attempt: int,
                                system_prompt=None):
            return await super().generate_clip(item_id=item_id, tier=tier, seconds=seconds,
                                               attempt=attempt, system_prompt=system_prompt)

    dec = DecoratedMock(tiers=tiers)
    assert isinstance(dec, VideoPort), "isinstance(dec, VideoPort) falhou após @traced"
