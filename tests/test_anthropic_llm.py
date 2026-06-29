"""Testes offline do AnthropicLLMAdapter.

Todos os testes usam um cliente fake (sem rede, sem chave real).
O cliente fake é injetado via o construtor ``client=`` do adapter.

Cobertura:
1. generate_concepts retorna n conceitos com todas as chaves corretas,
   hook_style e format dentro dos enums, e offer propagado.
2. generate_concepts com bias envia os estilos no prompt enviado ao modelo.
3. write_script retorna a string esperada e inclui a plataforma no prompt.
4. stop_reason=="refusal" levanta RuntimeError em ambos os métodos.
"""
from __future__ import annotations

import json
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import orchestrator.adapters.anthropic_llm as anthropic_llm_module
from orchestrator.adapters.anthropic_llm import (
    AnthropicLLMAdapter,
    build_anthropic_llm_adapter,
    build_vercel_gateway_llm_adapter,
)

# Enums válidos (espelham o adapter)
_HOOK_STYLES = ["problem", "curiosity", "bold_claim", "emotional", "social_proof"]
_FORMATS = ["talking_head", "demo", "reaction"]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_text_block(text: str) -> types.SimpleNamespace:
    """Cria um bloco de texto simples como o SDK retorna."""
    return types.SimpleNamespace(type="text", text=text)


def _make_thinking_block() -> types.SimpleNamespace:
    """Cria um bloco de thinking (não-text) para testar iteração correta."""
    return types.SimpleNamespace(type="thinking", thinking="<thinking>...")


def _make_response(
    content: list[types.SimpleNamespace],
    stop_reason: str = "end_turn",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


def _make_fake_client(response: types.SimpleNamespace) -> MagicMock:
    """Retorna um fake AsyncAnthropic com messages.create como AsyncMock."""
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


def _concepts_json(n: int, offer: str = "serum X") -> str:
    """Gera JSON de resposta com n conceitos válidos."""
    concepts = [
        {
            "id": f"concept-{i:04d}",
            "offer": offer,
            "hook": f"hook line {i}",
            "angle": _HOOK_STYLES[i % len(_HOOK_STYLES)],
            "hook_style": _HOOK_STYLES[i % len(_HOOK_STYLES)],
            "format": _FORMATS[i % len(_FORMATS)],
        }
        for i in range(n)
    ]
    return json.dumps({"concepts": concepts})


# --------------------------------------------------------------------------- #
# Teste 1 — generate_concepts: estrutura e chaves obrigatórias               #
# --------------------------------------------------------------------------- #


async def test_generate_concepts_returns_n_with_correct_shape() -> None:
    offer = "serum X"
    n = 5
    fake_response = _make_response([_make_text_block(_concepts_json(n, offer))])
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    concepts = await adapter.generate_concepts(offer=offer, n=n, seed="abc123")

    assert len(concepts) == n
    required_keys = {"id", "offer", "hook", "angle", "hook_style", "format"}
    for c in concepts:
        assert required_keys.issubset(c.keys()), f"Missing keys in concept: {c}"
        assert c["hook_style"] in _HOOK_STYLES, f"Invalid hook_style: {c['hook_style']}"
        assert c["format"] in _FORMATS, f"Invalid format: {c['format']}"
        assert c["offer"] == offer, f"offer not propagated: {c['offer']}"


async def test_generate_concepts_propagates_offer_even_if_model_omits_it() -> None:
    """Se o modelo não incluir 'offer', o adapter deve preenchê-lo."""
    offer = "produto Y"
    # Conceitos sem campo 'offer' (como se o modelo tivesse omitido)
    concepts_no_offer = [
        {
            "id": f"concept-{i:04d}",
            "hook": f"hook {i}",
            "angle": _HOOK_STYLES[i % len(_HOOK_STYLES)],
            "hook_style": _HOOK_STYLES[i % len(_HOOK_STYLES)],
            "format": _FORMATS[i % len(_FORMATS)],
        }
        for i in range(3)
    ]
    raw_json = json.dumps({"concepts": concepts_no_offer})
    fake_response = _make_response([_make_text_block(raw_json)])
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    concepts = await adapter.generate_concepts(offer=offer, n=3, seed="seed1")

    for c in concepts:
        assert c["offer"] == offer


async def test_generate_concepts_truncates_to_n() -> None:
    """Se o modelo retornar mais que n, deve truncar."""
    n = 3
    offer = "extra test"
    # Modelo "retorna" 6 conceitos
    fake_response = _make_response([_make_text_block(_concepts_json(6, offer))])
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    concepts = await adapter.generate_concepts(offer=offer, n=n, seed="x")

    assert len(concepts) == n


async def test_generate_concepts_handles_thinking_blocks() -> None:
    """Deve ignorar blocos thinking e pegar apenas o bloco text."""
    offer = "serum Z"
    n = 2
    fake_response = _make_response([
        _make_thinking_block(),
        _make_text_block(_concepts_json(n, offer)),
    ])
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    concepts = await adapter.generate_concepts(offer=offer, n=n, seed="s1")

    assert len(concepts) == n


# --------------------------------------------------------------------------- #
# Teste 2 — generate_concepts com bias: bias vai no prompt                   #
# --------------------------------------------------------------------------- #


async def test_generate_concepts_bias_included_in_prompt() -> None:
    """Com bias, os hook_styles vencedores devem aparecer no prompt enviado."""
    offer = "serum X"
    bias = ["problem", "emotional"]
    n = 4
    fake_response = _make_response([_make_text_block(_concepts_json(n, offer))])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    await adapter.generate_concepts(offer=offer, n=n, seed="s", bias=bias)

    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    # O prompt do usuário deve citar os estilos de bias
    messages = call_kwargs["messages"]
    user_content = messages[0]["content"]
    for style in bias:
        assert style in user_content, (
            f"bias style '{style}' not found in prompt: {user_content!r}"
        )


async def test_generate_concepts_no_bias_asks_for_spread() -> None:
    """Sem bias, o prompt deve solicitar spread amplo entre os estilos."""
    offer = "product A"
    n = 3
    fake_response = _make_response([_make_text_block(_concepts_json(n, offer))])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    await adapter.generate_concepts(offer=offer, n=n, seed="s")

    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    user_content = call_kwargs["messages"][0]["content"]
    # Deve mencionar "spread" ou listar os 5 estilos
    assert "spread" in user_content.lower() or "problem" in user_content, (
        f"Expected spread instruction not found in prompt: {user_content!r}"
    )


async def test_generate_concepts_invalid_bias_styles_ignored() -> None:
    """Estilos de bias inválidos devem ser ignorados silenciosamente."""
    offer = "serum X"
    n = 2
    fake_response = _make_response([_make_text_block(_concepts_json(n, offer))])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    # "invalid_style" não está nos 5 válidos — não deve explodir
    concepts = await adapter.generate_concepts(
        offer=offer, n=n, seed="s", bias=["invalid_style", "problem"]
    )

    assert len(concepts) == n
    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    user_content = call_kwargs["messages"][0]["content"]
    # "invalid_style" não deve aparecer no prompt (foi filtrado)
    assert "invalid_style" not in user_content


# --------------------------------------------------------------------------- #
# Teste 3 — write_script: retorna string e calibra por plataforma            #
# --------------------------------------------------------------------------- #


async def test_write_script_returns_expected_text() -> None:
    script_text = "HOOK: Você já tentou isso?\nBODY: ...\nCTA: Clica no link!"
    fake_response = _make_response([_make_text_block(script_text)])
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    concept = {
        "id": "concept-0001",
        "offer": "serum X",
        "hook": "Você já tentou isso?",
        "angle": "problem",
        "hook_style": "problem",
        "format": "talking_head",
    }
    result = await adapter.write_script(
        concept=concept, creator_ref="creator-001", platform="tiktok"
    )

    assert result == script_text


async def test_write_script_includes_platform_in_prompt() -> None:
    """A plataforma deve aparecer no prompt enviado ao modelo."""
    platform = "tiktok"
    fake_response = _make_response([_make_text_block("HOOK: ...\nBODY: ...\nCTA: ...")])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    concept = {
        "id": "concept-0002",
        "offer": "serum X",
        "hook": "hook line",
        "angle": "bold_claim",
        "hook_style": "bold_claim",
        "format": "demo",
    }
    await adapter.write_script(concept=concept, creator_ref="creator-002", platform=platform)

    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    user_content = call_kwargs["messages"][0]["content"]
    assert platform in user_content.lower(), (
        f"Platform '{platform}' not found in prompt: {user_content!r}"
    )


async def test_write_script_tiktok_uses_fast_pacing() -> None:
    """TikTok deve gerar instrução de pacing FAST no prompt."""
    fake_response = _make_response([_make_text_block("HOOK: test\nBODY: ...\nCTA: ...")])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    concept = {"id": "c-001", "offer": "X", "hook": "h", "angle": "problem",
               "hook_style": "problem", "format": "talking_head"}
    await adapter.write_script(concept=concept, creator_ref="ref", platform="TikTok")

    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    user_content = call_kwargs["messages"][0]["content"]
    assert "FAST" in user_content, (
        f"Expected 'FAST' pacing for TikTok, not found in prompt: {user_content!r}"
    )


async def test_write_script_non_tiktok_uses_medium_pacing() -> None:
    """Plataformas não-TikTok devem usar pacing MEDIUM."""
    fake_response = _make_response([_make_text_block("HOOK: ...\nBODY: ...\nCTA: ...")])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    concept = {"id": "c-002", "offer": "Y", "hook": "h", "angle": "curiosity",
               "hook_style": "curiosity", "format": "demo"}
    await adapter.write_script(concept=concept, creator_ref="ref", platform="youtube")

    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    user_content = call_kwargs["messages"][0]["content"]
    assert "MEDIUM" in user_content, (
        f"Expected 'MEDIUM' pacing for youtube, not found in prompt: {user_content!r}"
    )


async def test_write_script_includes_thinking_block() -> None:
    """Deve ignorar bloco thinking e retornar apenas o texto."""
    script_text = "HOOK: Real hook\nBODY: ...\nCTA: ..."
    fake_response = _make_response([
        _make_thinking_block(),
        _make_text_block(script_text),
    ])
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    concept = {"id": "c-003", "offer": "Z", "hook": "h", "angle": "emotional",
               "hook_style": "emotional", "format": "reaction"}
    result = await adapter.write_script(concept=concept, creator_ref="ref", platform="instagram")

    assert result == script_text


# --------------------------------------------------------------------------- #
# Teste 4 — stop_reason == "refusal" levanta RuntimeError                    #
# --------------------------------------------------------------------------- #


async def test_generate_concepts_raises_on_refusal() -> None:
    fake_response = _make_response(
        content=[_make_text_block("I cannot help with that.")],
        stop_reason="refusal",
    )
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    with pytest.raises(RuntimeError, match="refused"):
        await adapter.generate_concepts(offer="bad offer", n=3, seed="s")


async def test_write_script_raises_on_refusal() -> None:
    fake_response = _make_response(
        content=[_make_text_block("I cannot help with that.")],
        stop_reason="refusal",
    )
    adapter = AnthropicLLMAdapter(client=_make_fake_client(fake_response))

    concept = {"id": "c-bad", "offer": "bad", "hook": "h", "angle": "problem",
               "hook_style": "problem", "format": "talking_head"}
    with pytest.raises(RuntimeError, match="refused"):
        await adapter.write_script(concept=concept, creator_ref="ref", platform="tiktok")


# --------------------------------------------------------------------------- #
# Teste 5 — construtor e fábrica                                             #
# --------------------------------------------------------------------------- #


def test_default_model_is_opus_4_8() -> None:
    """Sem argumentos, o modelo padrão deve ser claude-opus-4-8."""
    # Criamos sem client para verificar o atributo; sem chamar a API
    # (precisamos de ANTHROPIC_API_KEY no ambiente para criar AsyncAnthropic real,
    # mas podemos mockar no nível do construtor)
    fake_client = MagicMock()
    adapter = AnthropicLLMAdapter(client=fake_client)
    assert adapter.model == "claude-opus-4-8"


def test_custom_model_override() -> None:
    fake_client = MagicMock()
    adapter = AnthropicLLMAdapter(model="claude-sonnet-4-5", client=fake_client)
    assert adapter.model == "claude-sonnet-4-5"


def test_build_anthropic_llm_adapter_factory() -> None:
    """Fábrica deve retornar AnthropicLLMAdapter com o modelo correto."""
    # Patch do os.environ para que AsyncAnthropic() não precise de chave real
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")
    adapter = build_anthropic_llm_adapter({"llm_model": "claude-opus-4-8"})
    assert isinstance(adapter, AnthropicLLMAdapter)
    assert adapter.model == "claude-opus-4-8"


def test_build_anthropic_llm_adapter_uses_default_model() -> None:
    """Fábrica sem 'llm_model' no pipeline usa o padrão."""
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-placeholder")
    adapter = build_anthropic_llm_adapter({})
    assert adapter.model == "claude-opus-4-8"


def test_build_vercel_gateway_llm_adapter_uses_gateway_api_key(monkeypatch) -> None:
    class FakeAsyncAnthropic:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            self.api_key = api_key
            self.base_url = base_url

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://ai-gateway.vercel.sh")
    monkeypatch.setenv("AI_GATEWAY_LLM_MODEL", "anthropic/claude-opus-4.8")
    monkeypatch.setattr(anthropic_llm_module, "AsyncAnthropic", FakeAsyncAnthropic)

    adapter = build_vercel_gateway_llm_adapter({})

    assert isinstance(adapter, AnthropicLLMAdapter)
    assert adapter.model == "anthropic/claude-opus-4.8"
    assert adapter._client.api_key == "test-gateway-key"
    assert adapter._client.base_url == "https://ai-gateway.vercel.sh"


def test_build_vercel_gateway_llm_adapter_accepts_vercel_oidc_token(monkeypatch) -> None:
    class FakeAsyncAnthropic:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            self.api_key = api_key
            self.base_url = base_url

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "test-oidc-token")
    monkeypatch.delenv("AI_GATEWAY_LLM_MODEL", raising=False)
    monkeypatch.setattr(anthropic_llm_module, "AsyncAnthropic", FakeAsyncAnthropic)

    adapter = build_vercel_gateway_llm_adapter({})

    assert isinstance(adapter, AnthropicLLMAdapter)
    assert adapter.model == "anthropic/claude-opus-4.8"
    assert adapter._client.api_key == "test-oidc-token"


def test_build_vercel_gateway_llm_adapter_requires_auth_env(monkeypatch) -> None:
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)

    with pytest.raises(
        RuntimeError,
        match="AI_GATEWAY_API_KEY or VERCEL_OIDC_TOKEN",
    ):
        build_vercel_gateway_llm_adapter({})


def test_build_vercel_gateway_llm_adapter_model_fallback_prefers_env(monkeypatch) -> None:
    class FakeAsyncAnthropic:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            self.api_key = api_key
            self.base_url = base_url

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("AI_GATEWAY_LLM_MODEL", "anthropic/claude-opus-4.8")
    monkeypatch.setattr(anthropic_llm_module, "AsyncAnthropic", FakeAsyncAnthropic)

    adapter = build_vercel_gateway_llm_adapter({"llm_model": "pipeline-model"})

    assert adapter.model == "anthropic/claude-opus-4.8"


def test_build_vercel_gateway_llm_adapter_model_fallback_uses_pipeline(monkeypatch) -> None:
    class FakeAsyncAnthropic:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            self.api_key = api_key
            self.base_url = base_url

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AI_GATEWAY_LLM_MODEL", raising=False)
    monkeypatch.setattr(anthropic_llm_module, "AsyncAnthropic", FakeAsyncAnthropic)

    adapter = build_vercel_gateway_llm_adapter({"llm_model": "pipeline-model"})

    assert adapter.model == "pipeline-model"


def test_build_vercel_gateway_llm_adapter_model_fallback_uses_default(monkeypatch) -> None:
    class FakeAsyncAnthropic:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            self.api_key = api_key
            self.base_url = base_url

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.delenv("AI_GATEWAY_LLM_MODEL", raising=False)
    monkeypatch.setattr(anthropic_llm_module, "AsyncAnthropic", FakeAsyncAnthropic)

    adapter = build_vercel_gateway_llm_adapter({})

    assert adapter.model == "anthropic/claude-opus-4.8"


# --------------------------------------------------------------------------- #
# Teste 6 — output_config e thinking são passados na chamada                 #
# --------------------------------------------------------------------------- #


async def test_generate_concepts_passes_output_config_and_thinking() -> None:
    """messages.create deve receber output_config e thinking corretos."""
    n = 2
    offer = "test"
    fake_response = _make_response([_make_text_block(_concepts_json(n, offer))])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    await adapter.generate_concepts(offer=offer, n=n, seed="s")

    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    assert "output_config" in call_kwargs, "output_config not passed to messages.create"
    assert call_kwargs["thinking"] == {"type": "adaptive"}, (
        f"thinking param wrong: {call_kwargs.get('thinking')}"
    )
    # temperature, top_p, top_k, budget_tokens NÃO devem estar presentes
    for forbidden in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert forbidden not in call_kwargs, (
            f"Forbidden param '{forbidden}' found in messages.create kwargs"
        )


async def test_write_script_passes_thinking_no_forbidden_params() -> None:
    """write_script não deve passar temperature/top_p/top_k/budget_tokens."""
    fake_response = _make_response([_make_text_block("HOOK: ...\nBODY: ...\nCTA: ...")])
    fake_client = _make_fake_client(fake_response)
    adapter = AnthropicLLMAdapter(client=fake_client)

    concept = {"id": "c", "offer": "o", "hook": "h", "angle": "problem",
               "hook_style": "problem", "format": "talking_head"}
    await adapter.write_script(concept=concept, creator_ref="ref", platform="tiktok")

    call_kwargs: dict[str, Any] = fake_client.messages.create.call_args.kwargs
    assert call_kwargs["thinking"] == {"type": "adaptive"}
    for forbidden in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert forbidden not in call_kwargs, (
            f"Forbidden param '{forbidden}' found in write_script kwargs"
        )
