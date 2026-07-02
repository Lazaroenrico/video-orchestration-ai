"""Testes offline dos adapters reais do Creator (Step 3).

Todos os testes usam ``httpx.MockTransport`` — sem rede real, sem chave real.
Os handlers validam método, rota e headers antes de retornar respostas mock.
"""
from __future__ import annotations

import json

import httpx
import pytest

from orchestrator import media_store
from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters.creator_real import RealCreatorAdapter, build_real_creator_adapter
from orchestrator.adapters.elevenlabs_voice import ElevenLabsVoiceAdapter
from orchestrator.adapters.openai_image import (
    OpenAIImageAdapter,
    build_openai_image_vercel_adapter,
)
from orchestrator.adapters.topaz_upscale import TopazUpscaleAdapter
from orchestrator.graph.state import Artifact, Item

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_OPENAI = "https://api.openai.com/v1"
BASE_TOPAZ = "https://api.topazlabs.com/v1"
BASE_ELEVENLABS = "https://api.elevenlabs.io/v1"

FAKE_TOKEN = "test-token-123"
FAKE_FACE_URL = "https://cdn.openai.com/face-primary.png"
FAKE_UPSCALED_URL = "https://cdn.topazlabs.com/face-4k.png"
FAKE_VOICE_ID = "voice-abc123"


def _make_openai_transport(expected_index: int) -> httpx.MockTransport:
    """MockTransport que valida a chamada ao endpoint de geração de imagem OpenAI."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST", f"Esperado POST, recebido {request.method}"
        assert str(request.url) == f"{BASE_OPENAI}/images/generations", (
            f"URL incorreta: {request.url}"
        )
        assert "Bearer" in request.headers.get("authorization", ""), (
            "Header Authorization ausente ou inválido"
        )
        body = json.loads(request.content)
        assert body["model"] == "gpt-image-2"
        assert f"creator-{expected_index}" in body["prompt"]
        return httpx.Response(200, json={"data": [{"url": FAKE_FACE_URL}]})

    return httpx.MockTransport(handler)


def _make_topaz_transport(expected_image_url: str) -> httpx.MockTransport:
    """MockTransport que valida a chamada ao endpoint de upscale Topaz."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST", f"Esperado POST, recebido {request.method}"
        assert str(request.url) == f"{BASE_TOPAZ}/upscale", (
            f"URL incorreta: {request.url}"
        )
        assert "Bearer" in request.headers.get("authorization", ""), (
            "Header Authorization ausente ou inválido"
        )
        body = json.loads(request.content)
        assert body["image_url"] == expected_image_url
        assert body["scale"] == 4
        return httpx.Response(200, json={"output_url": FAKE_UPSCALED_URL})

    return httpx.MockTransport(handler)


def _make_elevenlabs_transport(expected_index: int) -> httpx.MockTransport:
    """MockTransport que valida a chamada ao endpoint de criação de voz ElevenLabs."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST", f"Esperado POST, recebido {request.method}"
        assert str(request.url) == f"{BASE_ELEVENLABS}/voices/add", (
            f"URL incorreta: {request.url}"
        )
        assert request.headers.get("xi-api-key"), "Header xi-api-key ausente"
        body = json.loads(request.content)
        assert body["name"] == f"creator-{expected_index}"
        return httpx.Response(200, json={"voice_id": FAKE_VOICE_ID})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Testes isolados: OpenAIImageAdapter
# ---------------------------------------------------------------------------


async def test_openai_image_generate_face_returns_correct_shape() -> None:
    """generate_face deve retornar primary URL e lista de 5 ângulos canônicos."""
    transport = _make_openai_transport(expected_index=2)
    client = httpx.AsyncClient(transport=transport, base_url=BASE_OPENAI)
    adapter = OpenAIImageAdapter(base_url=BASE_OPENAI, token=FAKE_TOKEN, client=client)

    result = await adapter.generate_face(2)

    assert result["primary"] == FAKE_FACE_URL
    assert result["angles"] == ["front", "3/4", "profile", "smile", "neutral"]


async def test_openai_image_sends_correct_endpoint_and_auth() -> None:
    """generate_face deve chamar /images/generations com Authorization: Bearer."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://example.com/img.png"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_OPENAI)
    adapter = OpenAIImageAdapter(base_url=BASE_OPENAI, token="my-key", client=client)

    await adapter.generate_face(0)

    assert len(calls) == 1
    req = calls[0]
    assert str(req.url).endswith("/images/generations")
    assert req.headers["authorization"] == "Bearer my-key"
    body = json.loads(req.content)
    assert body["model"] == "gpt-image-2"


async def test_openai_image_raises_on_http_error() -> None:
    """generate_face deve propagar erro HTTP (raise_for_status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_OPENAI)
    adapter = OpenAIImageAdapter(base_url=BASE_OPENAI, token="bad-key", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.generate_face(0)


async def test_openai_image_logs_error_body(caplog: pytest.LogCaptureFixture) -> None:
    """Em falha HTTP, o corpo da resposta do gateway é logado para diagnóstico.

    O 400 do Vercel AI Gateway só faz sentido com o corpo (causa real); sem este log
    o raise_for_status() esconde o motivo. O corpo de erro é JSON curto — sem base64.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "prompt rejeitado"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_OPENAI)
    adapter = OpenAIImageAdapter(base_url=BASE_OPENAI, token=FAKE_TOKEN, client=client)

    with caplog.at_level("ERROR", logger="orchestrator.adapters.openai_image"):
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.generate_face(0)

    assert any("status=400" in r.message and "prompt rejeitado" in r.message for r in caplog.records)


async def test_openai_image_http_error_includes_response_body() -> None:
    """A exceção HTTP deve incluir o corpo do gateway para diagnóstico no LangGraph."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Model 'openai/gpt-image-2' is not available"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_OPENAI)
    adapter = OpenAIImageAdapter(base_url=BASE_OPENAI, token=FAKE_TOKEN, client=client)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await adapter.generate_face(0)

    message = str(excinfo.value)
    assert "openai_image: 400 Bad Request" in message
    assert "Model 'openai/gpt-image-2' is not available" in message
    assert "/images/generations" in message


async def test_openai_image_wraps_custom_prompt_with_safety_guardrails() -> None:
    """Prompt customizado deve virar briefing dentro de um prompt seguro de retrato."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": FAKE_FACE_URL}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_OPENAI)
    adapter = OpenAIImageAdapter(base_url=BASE_OPENAI, token=FAKE_TOKEN, client=client)

    await adapter.generate_face(3, system_prompt="Influencer feminina, 25 anos. Look casual.")

    body = json.loads(calls[0].content)
    prompt = body["prompt"]
    assert "adult professional UGC creator" in prompt
    assert "modest everyday clothing" in prompt
    assert "head-and-shoulders portrait" in prompt
    assert "conservative commercial profile portrait" in prompt
    assert "brand-safe product review context" in prompt
    assert "Influencer feminina, 25 anos. Look casual." in prompt
    assert "creator-3" in prompt


async def test_openai_image_safe_prompt_avoids_explicit_sensitive_terms() -> None:
    """Guardrails de imagem devem ser positivas, sem termos que acionem safety por texto."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": FAKE_FACE_URL}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_OPENAI)
    adapter = OpenAIImageAdapter(base_url=BASE_OPENAI, token=FAKE_TOKEN, client=client)

    await adapter.generate_face(4, system_prompt="Criadora UGC para skincare, look casual.")

    body = json.loads(calls[0].content)
    prompt = body["prompt"].lower()
    for blocked in ("sexual", "nudity", "lingerie", "swimwear", "erotic"):
        assert blocked not in prompt


# ---------------------------------------------------------------------------
# Testes isolados: TopazUpscaleAdapter
# ---------------------------------------------------------------------------


async def test_topaz_upscale_returns_output_url() -> None:
    """upscale deve retornar a string output_url da resposta."""
    transport = _make_topaz_transport(expected_image_url=FAKE_FACE_URL)
    client = httpx.AsyncClient(transport=transport, base_url=BASE_TOPAZ)
    adapter = TopazUpscaleAdapter(base_url=BASE_TOPAZ, token=FAKE_TOKEN, client=client)

    result = await adapter.upscale(FAKE_FACE_URL)

    assert result == FAKE_UPSCALED_URL


async def test_topaz_upscale_sends_correct_endpoint_and_auth() -> None:
    """upscale deve chamar /upscale com Authorization: Bearer e scale=4."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"output_url": "https://topaz.example.com/out.png"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_TOPAZ)
    adapter = TopazUpscaleAdapter(base_url=BASE_TOPAZ, token="topaz-key", client=client)

    await adapter.upscale("https://source.example.com/img.png")

    assert len(calls) == 1
    req = calls[0]
    assert str(req.url).endswith("/upscale")
    assert req.headers["authorization"] == "Bearer topaz-key"
    body = json.loads(req.content)
    assert body["scale"] == 4
    assert body["image_url"] == "https://source.example.com/img.png"


async def test_topaz_upscale_raises_on_http_error() -> None:
    """upscale deve propagar erro HTTP (raise_for_status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_TOPAZ)
    adapter = TopazUpscaleAdapter(base_url=BASE_TOPAZ, token=FAKE_TOKEN, client=client)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.upscale("https://source.example.com/img.png")


# ---------------------------------------------------------------------------
# Testes isolados: ElevenLabsVoiceAdapter
# ---------------------------------------------------------------------------


async def test_elevenlabs_create_voice_returns_voice_id() -> None:
    """create_voice deve retornar a string voice_id da resposta."""
    transport = _make_elevenlabs_transport(expected_index=3)
    client = httpx.AsyncClient(transport=transport, base_url=BASE_ELEVENLABS)
    adapter = ElevenLabsVoiceAdapter(base_url=BASE_ELEVENLABS, token=FAKE_TOKEN, client=client)

    result = await adapter.create_voice(3)

    assert result == FAKE_VOICE_ID


async def test_elevenlabs_sends_correct_endpoint_and_auth() -> None:
    """create_voice deve chamar /voices/add com xi-api-key e name correto."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"voice_id": "v-test"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_ELEVENLABS)
    adapter = ElevenLabsVoiceAdapter(base_url=BASE_ELEVENLABS, token="el-key", client=client)

    await adapter.create_voice(7)

    assert len(calls) == 1
    req = calls[0]
    assert str(req.url).endswith("/voices/add")
    assert req.headers["xi-api-key"] == "el-key"
    body = json.loads(req.content)
    assert body["name"] == "creator-7"


async def test_elevenlabs_raises_on_http_error() -> None:
    """create_voice deve propagar erro HTTP (raise_for_status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_ELEVENLABS)
    adapter = ElevenLabsVoiceAdapter(base_url=BASE_ELEVENLABS, token="bad", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.create_voice(0)


async def test_elevenlabs_create_voice_includes_profile_description() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"voice_id": "v-profile"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_ELEVENLABS)
    adapter = ElevenLabsVoiceAdapter(base_url=BASE_ELEVENLABS, token=FAKE_TOKEN, client=client)

    await adapter.create_voice(
        2,
        voice_profile=VoiceProfile(preset="neutral", prompt="Calm and clear UGC narration."),
    )

    body = json.loads(calls[0].content)
    assert body["name"] == "creator-2"
    assert "Calm and clear UGC narration." in body["description"]
    assert body["labels"]["preset"] == "neutral"


# ---------------------------------------------------------------------------
# Testes de integração: RealCreatorAdapter
# ---------------------------------------------------------------------------


async def test_real_creator_build_creator_returns_correct_shape() -> None:
    """build_creator(2) deve retornar dict com id, angles, upscaled_base e voice_id."""
    index = 2

    image_adapter = OpenAIImageAdapter(
        base_url=BASE_OPENAI,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(
            transport=_make_openai_transport(expected_index=index),
            base_url=BASE_OPENAI,
        ),
    )
    topaz_adapter = TopazUpscaleAdapter(
        base_url=BASE_TOPAZ,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(
            transport=_make_topaz_transport(expected_image_url=FAKE_FACE_URL),
            base_url=BASE_TOPAZ,
        ),
    )
    voice_adapter = ElevenLabsVoiceAdapter(
        base_url=BASE_ELEVENLABS,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(
            transport=_make_elevenlabs_transport(expected_index=index),
            base_url=BASE_ELEVENLABS,
        ),
    )

    creator = RealCreatorAdapter(image=image_adapter, topaz=topaz_adapter, voice=voice_adapter)
    result = await creator.build_creator(index)

    assert result["id"] == "creator-2"
    assert result["angles"] == ["front", "3/4", "profile", "smile", "neutral"]
    assert result["upscaled_base"] == FAKE_UPSCALED_URL
    assert result["voice_id"] == FAKE_VOICE_ID


async def test_real_creator_composes_sub_adapters_correctly() -> None:
    """A URL primary do OpenAI deve ser passada para o Topaz (orquestração correta)."""
    primary_received_by_topaz: list[str] = []

    def openai_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://openai.example.com/face.png"}]})

    def topaz_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        primary_received_by_topaz.append(body["image_url"])
        return httpx.Response(200, json={"output_url": "https://topaz.example.com/upscaled.png"})

    def elevenlabs_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"voice_id": "v-xyz"})

    image_adapter = OpenAIImageAdapter(
        base_url=BASE_OPENAI,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(openai_handler), base_url=BASE_OPENAI),
    )
    topaz_adapter = TopazUpscaleAdapter(
        base_url=BASE_TOPAZ,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(topaz_handler), base_url=BASE_TOPAZ),
    )
    voice_adapter = ElevenLabsVoiceAdapter(
        base_url=BASE_ELEVENLABS,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(elevenlabs_handler), base_url=BASE_ELEVENLABS),
    )

    creator = RealCreatorAdapter(image=image_adapter, topaz=topaz_adapter, voice=voice_adapter)
    await creator.build_creator(0)

    assert primary_received_by_topaz == ["https://openai.example.com/face.png"]


async def test_real_creator_infers_voice_profile_from_system_prompt() -> None:
    captured: list[VoiceProfile | None] = []
    image = _FakeImage()

    class _VoiceSpy:
        async def create_voice(self, index: int, voice_profile: VoiceProfile | None = None) -> str:
            captured.append(voice_profile)
            return "voice-inferred"

    creator = RealCreatorAdapter(image=image, topaz=_OkUpscale(), voice=_VoiceSpy())

    result = await creator.build_creator(
        0, system_prompt="Criadora UGC feminina, tom caloroso e amigavel."
    )

    assert captured[0] is not None
    assert captured[0].preset == "female"
    assert "Criadora UGC feminina" in captured[0].prompt
    # Paridade: a imagem recebe o MESMO perfil que a voz.
    assert image.voice_profile_seen is captured[0]
    assert result["voice_profile"] == {
        "preset": "female",
        "prompt": "Criadora UGC feminina, tom caloroso e amigavel.",
    }


async def test_real_creator_explicit_voice_profile_overrides_inference() -> None:
    captured: list[VoiceProfile | None] = []

    class _VoiceSpy:
        async def create_voice(self, index: int, voice_profile: VoiceProfile | None = None) -> str:
            captured.append(voice_profile)
            return "voice-override"

    image = _FakeImage()
    creator = RealCreatorAdapter(image=image, topaz=_OkUpscale(), voice=_VoiceSpy())
    override = VoiceProfile(preset="male", prompt="Deep and grounded delivery.")

    result = await creator.build_creator(
        1,
        system_prompt="Criadora feminina para skincare.",
        voice_profile=override,
    )

    assert captured[0] == override
    # Paridade: override também vai para a imagem (não a inferência do texto).
    assert image.voice_profile_seen == override
    assert result["voice_profile"] == {
        "preset": "male",
        "prompt": "Deep and grounded delivery.",
    }


async def test_real_creator_implements_creator_port_protocol() -> None:
    """RealCreatorAdapter deve satisfazer o Protocol CreatorPort em runtime."""
    from orchestrator.adapters.base import CreatorPort

    def openai_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://example.com/f.png"}]})

    def topaz_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_url": "https://example.com/upscaled.png"})

    def elevenlabs_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"voice_id": "v-protocol"})

    image_adapter = OpenAIImageAdapter(
        base_url=BASE_OPENAI,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(openai_handler), base_url=BASE_OPENAI),
    )
    topaz_adapter = TopazUpscaleAdapter(
        base_url=BASE_TOPAZ,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(topaz_handler), base_url=BASE_TOPAZ),
    )
    voice_adapter = ElevenLabsVoiceAdapter(
        base_url=BASE_ELEVENLABS,
        token=FAKE_TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(elevenlabs_handler), base_url=BASE_ELEVENLABS),
    )

    creator = RealCreatorAdapter(image=image_adapter, topaz=topaz_adapter, voice=voice_adapter)
    assert isinstance(creator, CreatorPort)


async def test_build_real_creator_adapter_factory() -> None:
    """build_real_creator_adapter deve retornar um RealCreatorAdapter."""
    adapter = build_real_creator_adapter({})
    assert isinstance(adapter, RealCreatorAdapter)
    assert isinstance(adapter.image, OpenAIImageAdapter)
    assert isinstance(adapter.topaz, TopazUpscaleAdapter)
    assert isinstance(adapter.voice, ElevenLabsVoiceAdapter)


# ---------------------------------------------------------------------------
# Degradação graciosa: a face gerada nunca é perdida
# ---------------------------------------------------------------------------


class _FakeImage:
    def __init__(self, primary: str = "data:image/png;base64,AAAA") -> None:
        self.primary = primary

    async def generate_face(self, index: int, system_prompt=None, voice_profile=None) -> dict:
        self.voice_profile_seen = voice_profile
        return {"primary": self.primary, "angles": ["front", "3/4", "profile", "smile", "neutral"]}


class _BoomUpscale:
    async def upscale(self, image_url: str) -> str:
        raise RuntimeError("upscale indisponível")


class _OkUpscale:
    async def upscale(self, image_url: str) -> str:
        return "https://cdn/upscaled.png"


class _BoomVoice:
    async def create_voice(self, index: int, voice_profile: VoiceProfile | None = None) -> str:
        raise RuntimeError("voz indisponível")


class _OkVoice:
    async def create_voice(self, index: int, voice_profile: VoiceProfile | None = None) -> str:
        return "voice-xyz"


async def test_build_creator_falls_back_to_generated_face_when_upscale_fails() -> None:
    """Upscale falha → usa a face gerada (não-upscalada); creator não levanta."""
    creator = RealCreatorAdapter(image=_FakeImage(), topaz=_BoomUpscale(), voice=_OkVoice())
    result = await creator.build_creator(0)
    assert result["upscaled_base"] == "data:image/png;base64,AAAA"
    assert result["voice_id"] == "voice-xyz"


async def test_build_creator_falls_back_to_empty_voice_when_voice_fails() -> None:
    """Voz falha → voice_id vazio; imagem preservada, creator não levanta."""
    creator = RealCreatorAdapter(image=_FakeImage(), topaz=_OkUpscale(), voice=_BoomVoice())
    result = await creator.build_creator(0)
    assert result["upscaled_base"] == "https://cdn/upscaled.png"
    assert result["voice_id"] == ""


async def test_build_creator_propagates_when_face_generation_fails() -> None:
    """Sem face não há o que salvar → generate_face falhar deve propagar."""

    class _BoomImage:
        async def generate_face(self, index: int, system_prompt=None, voice_profile=None) -> dict:
            raise RuntimeError("image indisponível")

    creator = RealCreatorAdapter(image=_BoomImage(), topaz=_OkUpscale(), voice=_OkVoice())
    with pytest.raises(RuntimeError, match="image indisponível"):
        await creator.build_creator(0)


# ---------------------------------------------------------------------------
# Testes do GPT Image 2 via Vercel AI Gateway
# (contrato confirmado em https://vercel.com/docs/ai-gateway image-generation)
# ---------------------------------------------------------------------------


async def test_vercel_factory_uses_v1_base_url_and_prefixed_model(monkeypatch) -> None:
    """A factory do gateway deve usar base_url .../v1 (sem /openai) e model openai/gpt-image-2."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck_test")
    monkeypatch.delenv("AI_GATEWAY_OPENAI_BASE_URL", raising=False)

    adapter = build_openai_image_vercel_adapter({})

    assert adapter.base_url == "https://ai-gateway.vercel.sh/v1"
    assert adapter.model == "openai/gpt-image-2"
    assert adapter.token == "vck_test"


async def test_vercel_factory_respects_base_url_env_override(monkeypatch) -> None:
    """AI_GATEWAY_OPENAI_BASE_URL deve sobrescrever o base_url padrão."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck_test")
    monkeypatch.setenv("AI_GATEWAY_OPENAI_BASE_URL", "https://custom.gateway/v1")

    adapter = build_openai_image_vercel_adapter({})

    assert adapter.base_url == "https://custom.gateway/v1"


async def test_openai_image_parses_b64_json_into_data_uri() -> None:
    """Quando a resposta traz b64_json (gateway), primary deve virar um data URI."""
    fake_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": fake_b64}]})

    base = "https://ai-gateway.vercel.sh/v1"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base)
    adapter = OpenAIImageAdapter(
        base_url=base, token="vck_test", model="openai/gpt-image-2", client=client
    )

    result = await adapter.generate_face(0)

    assert result["primary"] == f"data:image/png;base64,{fake_b64}"
    assert result["angles"] == ["front", "3/4", "profile", "smile", "neutral"]


async def test_openai_image_sends_configured_model() -> None:
    """O body deve enviar o model configurado (ex.: openai/gpt-image-2 no gateway)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": "abc"}]})

    base = "https://ai-gateway.vercel.sh/v1"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base)
    adapter = OpenAIImageAdapter(
        base_url=base, token="vck_test", model="openai/gpt-image-2", client=client
    )

    await adapter.generate_face(0)

    body = json.loads(calls[0].content)
    assert body["model"] == "openai/gpt-image-2"
    assert str(calls[0].url).endswith("/images/generations")


async def test_vercel_factory_sets_generous_timeout(monkeypatch) -> None:
    """Geração de imagem é lenta — a factory deve configurar timeout generoso (>=60s)."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck_test")

    adapter = build_openai_image_vercel_adapter({})

    assert adapter.timeout >= 60.0


async def test_openai_image_default_timeout() -> None:
    """O adapter deve ter timeout padrão generoso para suportar geração de imagem."""
    adapter = OpenAIImageAdapter(token="t")
    assert adapter.timeout >= 60.0


async def test_openai_image_raises_when_no_url_or_b64() -> None:
    """Resposta sem url nem b64_json deve levantar erro claro."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{}]})

    base = "https://ai-gateway.vercel.sh/v1"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base)
    adapter = OpenAIImageAdapter(base_url=base, token="t", client=client)

    with pytest.raises(RuntimeError):
        await adapter.generate_face(0)


# ---------------------------------------------------------------------------
# ElevenLabsVoiceAdapter.synthesize_preview — amostra audível do voice_id
# ---------------------------------------------------------------------------

FAKE_PREVIEW_AUDIO = b"ID3\x03\x00\x00\x00\x00\x00\x00fake-mp3-bytes"


async def test_elevenlabs_synthesize_preview_returns_audio_bytes() -> None:
    """synthesize_preview deve retornar os bytes de áudio da resposta."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=FAKE_PREVIEW_AUDIO, headers={"content-type": "audio/mpeg"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_ELEVENLABS)
    adapter = ElevenLabsVoiceAdapter(base_url=BASE_ELEVENLABS, token=FAKE_TOKEN, client=client)

    result = await adapter.synthesize_preview(FAKE_VOICE_ID)

    assert result == FAKE_PREVIEW_AUDIO


async def test_elevenlabs_synthesize_preview_sends_correct_endpoint_and_auth() -> None:
    """synthesize_preview deve chamar /text-to-speech/{voice_id} com xi-api-key e texto."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=FAKE_PREVIEW_AUDIO)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_ELEVENLABS)
    adapter = ElevenLabsVoiceAdapter(base_url=BASE_ELEVENLABS, token="el-key", client=client)

    await adapter.synthesize_preview(FAKE_VOICE_ID)

    assert len(calls) == 1
    req = calls[0]
    assert str(req.url).endswith(f"/text-to-speech/{FAKE_VOICE_ID}")
    assert req.headers["xi-api-key"] == "el-key"
    body = json.loads(req.content)
    assert body["text"]


async def test_elevenlabs_synthesize_preview_raises_on_http_error() -> None:
    """synthesize_preview deve propagar erro HTTP (raise_for_status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE_ELEVENLABS)
    adapter = ElevenLabsVoiceAdapter(base_url=BASE_ELEVENLABS, token="bad", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.synthesize_preview(FAKE_VOICE_ID)


# ---------------------------------------------------------------------------
# media_store.persist_item_media — clips e assembled viram /media/... quando
# baixáveis; no-op total para mock:// (mesma garantia de persist_creator_media).
# ---------------------------------------------------------------------------

_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes"


def _ok_media_transport(content: bytes, content_type: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": content_type})

    return httpx.MockTransport(handler)


async def test_persist_item_media_downloads_clips_and_assembled(tmp_path) -> None:
    """clips[].uri e assembled.uri http(s) -> baixados para /media/{run}/items/{id}/..."""
    item = Item(
        id="item-1",
        concept={"hook_style": "problem"},
        clips=[
            Artifact(kind="clip", uri="https://cdn.example/clip0.mp4", meta={"tier": "ltx"}),
        ],
        assembled=Artifact(kind="final", uri="https://cdn.example/final.mp4"),
    )
    client = httpx.AsyncClient(transport=_ok_media_transport(_FAKE_MP4, "video/mp4"))

    out = await media_store.persist_item_media(
        item, run_id="run-9", media_root=tmp_path, client=client,
    )
    await client.aclose()

    assert out.clips[0].uri == "/media/run-9/items/item-1/clip-0.mp4"
    assert out.clips[0].meta["source_uri"] == "https://cdn.example/clip0.mp4"
    assert out.assembled.uri == "/media/run-9/items/item-1/assembled.mp4"
    assert out.assembled.meta["source_uri"] == "https://cdn.example/final.mp4"
    assert (tmp_path / "run-9" / "items" / "item-1" / "clip-0.mp4").read_bytes() == _FAKE_MP4
    assert (tmp_path / "run-9" / "items" / "item-1" / "assembled.mp4").read_bytes() == _FAKE_MP4
    # Item devolvido é renderável a partir das novas uris /media/...
    assert out.clips[0].uri.startswith("/media/")
    assert out.assembled.uri.startswith("/media/")


async def test_persist_item_media_mock_is_noop(tmp_path) -> None:
    """Item mock (clips/assembled mock://): nada baixado, item inalterado, sem disco."""
    item = Item(
        id="item-2",
        concept={"hook_style": "problem"},
        clips=[Artifact(kind="clip", uri="mock://clip/item-2/tier0")],
        assembled=Artifact(kind="final", uri="mock://final/item-2"),
    )

    out = await media_store.persist_item_media(item, run_id="run-9", media_root=tmp_path)

    assert out.clips[0].uri == "mock://clip/item-2/tier0"
    assert out.assembled.uri == "mock://final/item-2"
    assert "source_uri" not in out.clips[0].meta
    assert not any(tmp_path.iterdir())


async def test_persist_item_media_accepts_dict_input(tmp_path) -> None:
    """persist_item_media também aceita o item como dict (mirror do padrão do creator)."""
    item = {
        "id": "item-3",
        "clips": [{"kind": "clip", "uri": "https://cdn.example/clip0.mp4", "meta": {}}],
        "assembled": None,
    }
    client = httpx.AsyncClient(transport=_ok_media_transport(_FAKE_MP4, "video/mp4"))

    out = await media_store.persist_item_media(
        item, run_id="run-9", media_root=tmp_path, client=client,
    )
    await client.aclose()

    assert isinstance(out, dict)
    assert out["clips"][0]["uri"] == "/media/run-9/items/item-3/clip-0.mp4"
    assert out["clips"][0]["meta"]["source_uri"] == "https://cdn.example/clip0.mp4"


# ---------------------------------------------------------------------------
# Voice preview: creator com voice_id opaco (ElevenLabs) ganha voice_preview_uri
# audível via synthesize_preview; creator com voz já baixada (Replicate) reusa
# o path local já persistido.
# ---------------------------------------------------------------------------


class _StubVoiceWithPreview:
    async def create_voice(self, index: int, voice_profile: VoiceProfile | None = None) -> str:
        return "voice-opaque-id"

    async def synthesize_preview(self, voice_id: str) -> bytes:
        return FAKE_PREVIEW_AUDIO


async def test_build_voice_preview_synthesizes_for_opaque_voice_id(tmp_path) -> None:
    from orchestrator.nodes.stages import _build_voice_preview

    adapter = RealCreatorAdapter(image=_FakeImage(), topaz=_OkUpscale(), voice=_StubVoiceWithPreview())
    creator = {"id": "creator-0", "voice_id": "voice-opaque-id"}

    preview = await _build_voice_preview(adapter, creator, run_id="run-9", media_root=tmp_path)

    assert preview == "/media/run-9/creator-0/voice_preview.mp3"
    assert (tmp_path / "run-9" / "creator-0" / "voice_preview.mp3").read_bytes() == FAKE_PREVIEW_AUDIO


async def test_build_voice_preview_reuses_already_downloaded_voice(tmp_path) -> None:
    """Voz já persistida como áudio local (Replicate) -> preview é o próprio path local."""
    from orchestrator.nodes.stages import _build_voice_preview

    adapter = RealCreatorAdapter(image=_FakeImage(), topaz=_OkUpscale(), voice=_StubVoiceWithPreview())
    creator = {
        "id": "creator-1",
        "voice_id": "/media/run-9/creator-1/voice.wav",
        "voice_source_uri": "https://replicate.delivery/x/voice.wav",
    }

    preview = await _build_voice_preview(adapter, creator, run_id="run-9", media_root=tmp_path)

    assert preview == "/media/run-9/creator-1/voice.wav"
    # Nada de novo é sintetizado/persistido — reusa o path já existente.
    assert not any(tmp_path.iterdir())


async def test_build_voice_preview_noop_without_voice_subadapter(tmp_path) -> None:
    """Adapter sem sub-adapter de voz com synthesize_preview (ex.: MockAdapter) -> None."""
    from orchestrator.nodes.stages import _build_voice_preview

    class _NoVoiceAdapter:
        pass

    creator = {"id": "creator-0", "voice_id": "voice-0"}
    preview = await _build_voice_preview(
        _NoVoiceAdapter(), creator, run_id="run-9", media_root=tmp_path,
    )

    assert preview is None
    assert not any(tmp_path.iterdir())
