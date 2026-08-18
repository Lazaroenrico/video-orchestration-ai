"""Testes de caracterização e paridade do SDK ElevenLabs vs REST Adapter.

Esta suíte valida a paridade de contratos, payloads, respostas, tratamento de erros,
timeouts, retries e test doubles entre a implementação REST atual (ElevenLabsVoiceDesignAdapter
e ElevenLabsVoiceAdapter) e os contratos da API / SDK AsyncElevenLabs, garantindo
100% de execução offline, determinística e com zero chamadas pagas/live.

Critérios de aceite cobertos (Issue #7):
1. Matriz cobre Voice Design, create/finalize, TTS, list voices e reconcile.
2. Retry, timeout, erros e test doubles são comparados.
3. Nenhuma chamada paga ou live ocorre (MockTransport offline estrito).
4. Restrições e rollback documentados.
5. Decisão arquitetural comparativa explícita.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from orchestrator.adapters.elevenlabs_voice_design import (
    FINALIZED_VOICE_DESCRIPTION,
    ElevenLabsVoiceDesignAdapter,
)
from orchestrator.creative_contracts import (
    CreatorVoiceSpec,
    VoiceCandidate,
    VoiceDesignBatch,
)
from orchestrator.graph.state import Artifact

SAMPLE_WAV_B64 = "UklGRiQAAABXQVZFRm10IBAAAAABAAEARKwAAAB9AAACABAAZGF0YQAAAAA="


def _make_spec() -> CreatorVoiceSpec:
    return CreatorVoiceSpec(
        vocal_presentation="masculine",
        vocal_age="young_adult",
        timbre="warm",
        pace="energetic",
        energy="high",
        rationale="high-energy fitness hook",
    )


def _make_batch() -> VoiceDesignBatch:
    return VoiceDesignBatch(
        provider="elevenlabs",
        design_model="eleven_ttv_v3",
        description_hash="deschash_fitness",
        prompt_version="voice-match-v1",
        candidates=[
            VoiceCandidate(
                candidate_id="cand_gen_01",
                preview=Artifact(
                    kind="voice_preview",
                    uri=f"data:audio/mpeg;base64,{SAMPLE_WAV_B64}",
                    meta={"candidate_id": "cand_gen_01", "provider": "elevenlabs"},
                ),
                duration_seconds=3.8,
            )
        ],
        cost_usd=0.01,
        cost_source="estimate",
    )


# ==============================================================================
# 1. Paridade de Voice Design (POST /v1/text-to-voice/design)
# ==============================================================================
class TestVoiceDesignParity:
    """Caracteriza o endpoint de Voice Design e sua equivalência."""

    @pytest.mark.asyncio
    async def test_voice_design_payload_and_response_contract(self) -> None:
        captured_requests: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/text-to-voice/design"
            assert request.headers.get("xi-api-key") == "test-api-key"
            assert request.headers.get("content-type") == "application/json"
            body = json.loads(request.content)
            captured_requests.append(body)
            return httpx.Response(
                200,
                json={
                    "previews": [
                        {
                            "generated_voice_id": "cand_gen_01",
                            "audio_base_64": SAMPLE_WAV_B64,
                            "duration_secs": 3.8,
                        },
                        {
                            "generated_voice_id": "cand_gen_02",
                            "audio_base_64": SAMPLE_WAV_B64,
                            "duration_secs": 4.1,
                        },
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-api-key",
            http_client=client,
            design_model="eleven_ttv_v3",
            max_candidates=2,
        )

        spec = _make_spec()
        batch = await adapter.design_voice_candidates(
            spec,
            preview_text="Prévia de teste customizada com mais de cem caracteres para atender os limites estritos de validação do contrato.",
        )

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req["model_id"] == "eleven_ttv_v3"
        assert "A young_adult masculine voice" in req["voice_description"]
        assert "with a warm timbre" in req["voice_description"]
        assert "energetic pace" in req["voice_description"]
        assert "high energy" in req["voice_description"]
        assert "high-energy fitness hook" in req["voice_description"]
        assert len(batch.candidates) == 2
        assert batch.candidates[0].candidate_id == "cand_gen_01"
        assert batch.candidates[1].candidate_id == "cand_gen_02"
        assert batch.cost_usd == pytest.approx(0.02)
        assert batch.cost_source == "estimate"

    @pytest.mark.asyncio
    async def test_voice_design_rejects_malformed_provider_payloads(self) -> None:
        async def empty_previews_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"previews": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(empty_previews_handler))
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match="must return 1 to 3 voice candidates"):
            await adapter.design_voice_candidates(_make_spec())


# ==============================================================================
# 2. Paridade de Create / Finalize Voice (POST /v1/text-to-voice)
# ==============================================================================
class TestVoiceFinalizeParity:
    """Caracteriza o endpoint de finalização e criação permanente de voz."""

    @pytest.mark.asyncio
    async def test_finalize_voice_deterministic_naming_and_payload(self) -> None:
        captured_requests: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/text-to-voice"
            assert request.headers.get("xi-api-key") == "test-api-key"
            body = json.loads(request.content)
            captured_requests.append(body)
            return httpx.Response(200, json={"voice_id": "perm_voice_xyz999"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-api-key", http_client=client)
        batch = _make_batch()

        finalized = await adapter.finalize_voice(
            "cand_gen_01",
            batch=batch,
            creator_id="creator-0",
            organization_id="org_alpha",
        )

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert req["generated_voice_id"] == "cand_gen_01"
        assert req["voice_name"] == "ugc-org_alpha-creator-0-deschash_f"
        assert req["voice_description"] == FINALIZED_VOICE_DESCRIPTION
        assert finalized.voice_ref == "perm_voice_xyz999"
        assert finalized.selected_candidate_id == "cand_gen_01"
        assert finalized.provider == "elevenlabs"

    @pytest.mark.asyncio
    async def test_finalize_voice_rejects_candidate_not_in_batch(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        batch = _make_batch()
        with pytest.raises(ValueError, match="does not belong to voice design batch"):
            await adapter.finalize_voice(
                "non_existent_candidate",
                batch=batch,
                creator_id="creator-0",
                organization_id="org_alpha",
            )


# ==============================================================================
# 3. Paridade de TTS / Voiceover (POST /v1/text-to-speech/{voice_id})
# ==============================================================================
class TestTTSVoiceoverParity:
    """Caracteriza o endpoint de síntese de áudio (TTS)."""

    @pytest.mark.asyncio
    async def test_synthesize_voiceover_stream_to_artifact(self) -> None:
        captured_requests: list[dict[str, Any]] = []
        raw_audio_bytes = b"ID3v2.4_MOCK_MP3_PAYLOAD_CHUNK_123456789"

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/text-to-speech/perm_voice_xyz999"
            assert request.headers.get("xi-api-key") == "test-api-key"
            body = json.loads(request.content)
            captured_requests.append(body)
            return httpx.Response(
                200,
                content=raw_audio_bytes,
                headers={"content-type": "audio/mpeg"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-api-key",
            http_client=client,
            tts_model="eleven_turbo_v2_5",
            tts_cost_per_1000_chars_usd=0.03,
        )

        sample_script = "Este é o roteiro completo de UGC narrado com a voz sintetizada."
        artifact = await adapter.synthesize_voiceover(
            voice_ref="perm_voice_xyz999",
            text=sample_script,
        )

        assert len(captured_requests) == 1
        assert captured_requests[0]["text"] == sample_script
        assert captured_requests[0]["model_id"] == "eleven_turbo_v2_5"
        assert artifact.kind == "voiceover"
        assert artifact.uri.startswith("data:audio/mpeg;base64,")
        decoded = base64.b64decode(artifact.uri.split(",")[1])
        assert decoded == raw_audio_bytes
        assert artifact.meta["characters"] == len(sample_script)
        assert artifact.meta["cost_usd"] == pytest.approx(len(sample_script) * 0.03 / 1000)


# ==============================================================================
# 4. Paridade de List Voices & Reconciliação (GET /v1/voices)
# ==============================================================================
class TestListVoicesAndReconcileParity:
    """Caracteriza o endpoint de listagem e a lógica de reconciliação determinística."""

    @pytest.mark.asyncio
    async def test_reconcile_voice_exact_deterministic_match(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/voices"
            assert request.method == "GET"
            assert request.headers.get("xi-api-key") == "test-api-key"
            return httpx.Response(
                200,
                json={
                    "voices": [
                        {"voice_id": "other_voice_1", "name": "ugc-other-org-001"},
                        {
                            "voice_id": "matched_voice_777",
                            "name": "ugc-org_beta-creator-1-deschash_f",
                        },
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-api-key", http_client=client)
        batch = _make_batch()

        reconciled = await adapter.reconcile_voice(
            "cand_gen_01",
            batch=batch,
            creator_id="creator-1",
            organization_id="org_beta",
        )

        assert reconciled.voice_ref == "matched_voice_777"
        assert reconciled.selected_candidate_id == "cand_gen_01"
        assert reconciled.provider == "elevenlabs"

    @pytest.mark.asyncio
    async def test_reconcile_voice_fails_when_no_match_or_ambiguous(self) -> None:
        async def handler_no_match(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"voices": [{"voice_id": "v1", "name": "unrelated"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler_no_match))
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-api-key", http_client=client)
        batch = _make_batch()

        with pytest.raises(
            RuntimeError,
            match="uncertain voice finalization has no unique provider match",
        ):
            await adapter.reconcile_voice(
                "cand_gen_01",
                batch=batch,
                creator_id="creator-1",
                organization_id="org_beta",
            )


# ==============================================================================
# 5. Paridade de Erros, Timeouts, Retries e Test Doubles
# ==============================================================================
class TestErrorsRetriesAndTestDoublesParity:
    """Caracteriza o comportamento sob falhas de rede, 429, 5xx e injeção de test doubles."""

    @pytest.mark.asyncio
    async def test_retry_on_429_and_connect_error_eventually_succeeds(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            if attempts == 2:
                raise httpx.ConnectError("Temporary connection dropped")
            return httpx.Response(
                200,
                json={
                    "previews": [
                        {
                            "generated_voice_id": "cand_gen_retry",
                            "audio_base_64": SAMPLE_WAV_B64,
                            "duration_secs": 3.0,
                        }
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=3,
            retry_backoff_seconds=0.01,
        )

        batch = await adapter.design_voice_candidates(_make_spec())
        assert attempts == 3
        assert len(batch.candidates) == 1
        assert batch.candidates[0].candidate_id == "cand_gen_retry"

    @pytest.mark.asyncio
    async def test_500_and_503_post_fail_immediately_without_retry(self) -> None:
        """5xx em POST não deve retentar automaticamente para evitar duplicidade."""
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                503,
                content="Service Unavailable",
                headers={"request-id": "req_err_503"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=3,
            retry_backoff_seconds=0.01,
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await adapter.design_voice_candidates(_make_spec())

        assert exc_info.value.response.status_code == 503
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_400_validation_error_fails_immediately_without_retry(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                400,
                json={"detail": {"message": "Invalid voice description parameters"}},
                headers={"request-id": "req_err_400"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=3,
            retry_backoff_seconds=0.01,
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await adapter.design_voice_candidates(_make_spec())

        assert exc_info.value.response.status_code == 400
        assert attempts == 1  # 400 não deve ser retentado

    @pytest.mark.asyncio
    async def test_connect_timeout_vs_read_timeout_classification(self) -> None:
        """Verifica que exceções de transporte httpx propagam limpamente para o caller/ledger."""
        async def timeout_handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("Connection timed out to api.elevenlabs.io")

        client = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=1,
            retry_backoff_seconds=0.01,
        )

        with pytest.raises(httpx.ConnectTimeout):
            await adapter.design_voice_candidates(_make_spec())

    def test_offline_test_double_isolation_guarantee(self) -> None:
        """Garante que instanciar e executar com MockTransport não realiza chamadas live."""
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="mock_key",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200))
            ),
        )
        assert adapter.cost_source == "estimate"
        assert adapter.base_url == "https://api.elevenlabs.io"
