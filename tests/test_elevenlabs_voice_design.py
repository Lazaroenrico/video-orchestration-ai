from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orchestrator.adapters.elevenlabs_voice_design import ElevenLabsVoiceDesignAdapter
from orchestrator.creative_contracts import (
    CreatorVoiceSpec,
    VoiceCandidate,
    VoiceDesignBatch,
)
from orchestrator.graph.state import Artifact


def _spec() -> CreatorVoiceSpec:
    return CreatorVoiceSpec(
        vocal_presentation="feminine",
        vocal_age="adult",
        timbre="warm",
        pace="conversational",
        energy="balanced",
    )


def _batch() -> VoiceDesignBatch:
    return VoiceDesignBatch(
        provider="elevenlabs",
        design_model="eleven_ttv_v3",
        description_hash="deschash123",
        prompt_version="voice-match-v1",
        candidates=[
            VoiceCandidate(
                candidate_id="candidate-1",
                preview=Artifact(kind="voice_preview", uri="r2://preview.mp3"),
                duration_seconds=3.5,
            )
        ],
        cost_usd=0.01,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds must be positive"),
        ({"max_concurrency": 2}, "concurrency must be 1"),
        ({"max_candidates": 0}, "max_candidates must be between 1 and 3"),
        ({"max_retries": -1}, "max_retries must not be negative"),
        ({"design_cost_per_candidate_usd": -0.01}, "cost estimates"),
        ({"tts_cost_per_1000_chars_usd": -0.01}, "cost estimates"),
        ({"cost_source": "invoice"}, "cost_source=estimate"),
    ],
)
def test_elevenlabs_voice_design_rejects_unsafe_runtime_policies(
    kwargs: dict[str, Any], message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ElevenLabsVoiceDesignAdapter(**kwargs)


@pytest.mark.asyncio
async def test_elevenlabs_voice_design_adapter_calls_design_endpoint() -> None:
    payloads: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/text-to-voice/design":
            payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "previews": [
                        {
                            "generated_voice_id": "gen_voice_001",
                            "audio_base_64": "UklGRiQAAABXQVZFRm10IBAAAAABAAEARKwAAAB9AAACABAAZGF0YQAAAAA=",
                            "duration_secs": 3.5,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        batch = await adapter.design_voice_candidates(_spec())
        assert len(batch.candidates) == 1
        assert batch.candidates[0].candidate_id == "gen_voice_001"
        assert payloads[0]["model_id"] == "eleven_ttv_v3"


@pytest.mark.asyncio
async def test_elevenlabs_voice_design_adapter_calls_create_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/text-to-voice":
            return httpx.Response(
                200,
                json={"voice_id": "eleven_permanent_123"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        candidate = VoiceCandidate(
            candidate_id="gen_voice_001",
            preview=Artifact(kind="voice_preview", uri="r2://preview.mp3"),
            duration_seconds=3.5,
        )
        batch = VoiceDesignBatch(
            provider="elevenlabs",
            design_model="eleven_ttv_v3",
            description_hash="deschash123",
            prompt_version="voice-match-v1",
            candidates=[candidate],
            cost_usd=0.01,
        )
        finalized = await adapter.finalize_voice(
            "gen_voice_001",
            batch=batch,
            creator_id="creator-0",
            organization_id="org-test",
        )
        assert finalized.voice_ref == "eleven_permanent_123"


@pytest.mark.parametrize("preview_text", ["curto", "x" * 1001])
async def test_voice_design_rejects_preview_text_outside_provider_limits(
    preview_text: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid preview must fail before HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match="preview text must contain 100 to 1000"):
            await adapter.design_voice_candidates(_spec(), preview_text=preview_text)


@pytest.mark.parametrize("candidate_count", [0, 4])
async def test_voice_design_requires_one_to_three_provider_candidates(
    candidate_count: int,
) -> None:
    previews = [
        {
            "generated_voice_id": f"candidate-{index}",
            "audio_base_64": "UklGRg==",
            "duration_secs": 3.0,
        }
        for index in range(candidate_count)
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"previews": previews})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match="1 to 3 voice candidates"):
            await adapter.design_voice_candidates(_spec())


@pytest.mark.parametrize(
    "preview",
    [
        {"audio_base_64": "UklGRg==", "duration_secs": 3.0},
        {
            "generated_voice_id": "candidate-1",
            "audio_base_64": "not-base64!",
            "duration_secs": 3.0,
        },
    ],
)
async def test_voice_design_rejects_candidates_without_valid_id_and_audio(
    preview: dict,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"previews": [preview]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match="voice candidate"):
            await adapter.design_voice_candidates(_spec())


@pytest.mark.parametrize(
    ("previews", "message"),
    [
        (["not-an-object"], "must be an object"),
        (
            [
                {"generated_voice_id": "same", "audio_base_64": "UklGRg=="},
                {"generated_voice_id": "same", "audio_base_64": "UklGRg=="},
            ],
            "IDs must be unique",
        ),
        ([{"generated_voice_id": "candidate-1"}], "missing base64 audio"),
    ],
)
async def test_voice_design_rejects_malformed_candidate_collections(
    previews: list[Any], message: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"previews": previews})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match=message):
            await adapter.design_voice_candidates(_spec())


async def test_finalize_voice_rejects_response_without_permanent_voice_id() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    candidate = VoiceCandidate(
        candidate_id="candidate-1",
        preview=Artifact(kind="voice_preview", uri="r2://preview.mp3"),
        duration_seconds=3.0,
    )
    batch = VoiceDesignBatch(
        description_hash="description-hash",
        candidates=[candidate],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match="permanent voice_id"):
            await adapter.finalize_voice(
                "candidate-1",
                batch=batch,
                creator_id="creator-0",
                organization_id="org-1",
            )


async def test_finalize_voice_requires_candidate_from_the_same_batch() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("foreign candidate must fail before HTTP")

    candidate = VoiceCandidate(
        candidate_id="candidate-1",
        preview=Artifact(kind="voice_preview", uri="r2://preview.mp3"),
        duration_seconds=3.0,
    )
    batch = VoiceDesignBatch(
        description_hash="description-hash",
        candidates=[candidate],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match="does not belong to voice design batch"):
            await adapter.finalize_voice(
                "candidate-other",
                batch=batch,
                creator_id="creator-0",
                organization_id="org-1",
            )


async def test_voice_design_retries_429_then_reuses_the_same_request() -> None:
    requests: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(429, json={"detail": "throttled"})
        return httpx.Response(
            200,
            json={
                "previews": [
                    {
                        "generated_voice_id": "candidate-1",
                        "audio_base_64": "UklGRg==",
                        "duration_secs": 3.0,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=1,
            retry_backoff_seconds=0,
        )
        batch = await adapter.design_voice_candidates(_spec())

    assert batch.candidates[0].candidate_id == "candidate-1"
    assert len(requests) == 2
    assert requests[0] == requests[1]


async def test_voice_design_retries_connect_timeout_before_send() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("connect", request=request)
        return httpx.Response(
            200,
            json={
                "previews": [
                    {
                        "generated_voice_id": "candidate-1",
                        "audio_base_64": "UklGRg==",
                        "duration_secs": 3.0,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=1,
            retry_backoff_seconds=0,
        )
        batch = await adapter.design_voice_candidates(_spec())

    assert batch.candidates[0].candidate_id == "candidate-1"
    assert calls == 2


async def test_voice_design_does_not_retry_ambiguous_read_timeout() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=3,
            retry_backoff_seconds=0,
        )
        with pytest.raises(httpx.ReadTimeout):
            await adapter.design_voice_candidates(_spec())

    assert calls == 1


@pytest.mark.parametrize("status", [401, 422, 500])
async def test_voice_design_does_not_retry_non_throttle_http_errors(
    status: int,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"detail": "provider rejected request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(
            api_key="test-key",
            http_client=client,
            max_retries=3,
            retry_backoff_seconds=0,
        )
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await adapter.design_voice_candidates(_spec())

    assert raised.value.response.status_code == status
    assert calls == 1


async def test_voice_design_error_log_does_not_expose_provider_body_or_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "elevenlabs-secret-value"
    creative = "Oferta Serum X com roteiro privado"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            headers={"request-id": "req-safe-123"},
            json={"detail": f"{creative} {secret}"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key=secret, http_client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.design_voice_candidates(_spec())

    logs = caplog.text
    assert "status=422" in logs
    assert "request_id=req-safe-123" in logs
    assert creative not in logs
    assert secret not in logs


@pytest.mark.parametrize("matching_ids", [["voice-reconciled"], [], ["one", "two"]])
async def test_uncertain_finalization_reconciles_only_one_deterministic_name(
    matching_ids: list[str],
) -> None:
    expected_name = "ugc-org-test-creator-0-deschash12"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/voices"
        voices = [
            {"voice_id": voice_id, "name": expected_name}
            for voice_id in matching_ids
        ]
        voices.append({"voice_id": "other", "name": "another-voice"})
        return httpx.Response(200, json={"voices": voices})

    candidate = VoiceCandidate(
        candidate_id="candidate-1",
        preview=Artifact(kind="voice_preview", uri="r2://preview.mp3"),
        duration_seconds=3.0,
    )
    batch = VoiceDesignBatch(
        description_hash="deschash123",
        candidates=[candidate],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        if len(matching_ids) == 1:
            reconciled = await adapter.reconcile_voice(
                "candidate-1",
                batch=batch,
                creator_id="creator-0",
                organization_id="org-test",
            )
            assert reconciled.voice_ref == "voice-reconciled"
            assert reconciled.preview_uri == "r2://preview.mp3"
        else:
            with pytest.raises(RuntimeError, match="no unique provider match"):
                await adapter.reconcile_voice(
                    "candidate-1",
                    batch=batch,
                    creator_id="creator-0",
                    organization_id="org-test",
                )


async def test_reconcile_rejects_foreign_candidate_before_http() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("foreign candidate must fail before HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(ValueError, match="does not belong"):
            await adapter.reconcile_voice(
                "candidate-other",
                batch=_batch(),
                creator_id="creator-0",
                organization_id="org-test",
            )


async def test_reconcile_rejects_provider_match_without_voice_id() -> None:
    expected_name = "ugc-org-test-creator-0-deschash12"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"voices": [{"name": expected_name}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        with pytest.raises(RuntimeError, match="missing voice_id"):
            await adapter.reconcile_voice(
                "candidate-1",
                batch=_batch(),
                creator_id="creator-0",
                organization_id="org-test",
            )


def _patch_owned_client(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> list[httpx.AsyncClient]:
    import orchestrator.adapters.elevenlabs_voice_design as module

    real_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def build_client(*_args, **_kwargs):
        client = real_client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(module.httpx, "AsyncClient", build_client)
    return clients


async def test_owned_client_design_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "previews": [
                    {
                        "generated_voice_id": "candidate-1",
                        "audio_base_64": "UklGRg==",
                    }
                ]
            },
        )

    clients = _patch_owned_client(monkeypatch, handler)
    adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key")
    await adapter.design_voice_candidates(_spec())
    assert clients[0].is_closed


async def test_owned_client_finalize_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"voice_id": "voice-permanent"})

    clients = _patch_owned_client(monkeypatch, handler)
    adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key")
    await adapter.finalize_voice(
        "candidate-1",
        batch=_batch().model_dump(),
        creator_id="creator-0",
        organization_id="org-test",
    )
    assert clients[0].is_closed


async def test_owned_client_reconcile_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_name = "ugc-org-test-creator-0-deschash12"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"voices": [{"name": expected_name, "voice_id": "voice-1"}]},
        )

    clients = _patch_owned_client(monkeypatch, handler)
    adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key")
    await adapter.reconcile_voice(
        "candidate-1",
        batch=_batch(),
        creator_id="creator-0",
        organization_id="org-test",
    )
    assert clients[0].is_closed


async def test_tts_payload_cost_and_owned_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        assert request.url.path == "/v1/text-to-speech/voice-1"
        return httpx.Response(200, content=b"MP3")

    clients = _patch_owned_client(monkeypatch, handler)
    adapter = ElevenLabsVoiceDesignAdapter(
        api_key="test-key",
        tts_model="tts-model",
        tts_cost_per_1000_chars_usd=0.03,
    )
    artifact = await adapter.synthesize_voiceover(voice_ref="voice-1", text="abcd")

    assert payloads == [{"text": "abcd", "model_id": "tts-model"}]
    assert artifact.uri == "data:audio/mpeg;base64,TVAz"
    assert artifact.meta == {
        "provider": "elevenlabs",
        "voice_ref": "voice-1",
        "characters": 4,
        "cost_usd": pytest.approx(0.00012),
        "cost_source": "estimate",
    }
    assert clients[0].is_closed
