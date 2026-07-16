"""Integrity QC for live media artifacts."""
from __future__ import annotations

import pytest

from orchestrator.adapters.integrity_qc import IntegrityQCAdapter
from orchestrator.graph.state import Artifact, Item


def _item_with_clips(*clips: Artifact) -> Item:
    return Item(
        id="item-1",
        concept={"hook": "before-after"},
        script="HOOK: test\nCTA: buy now",
        clips=list(clips),
    )


async def test_integrity_qc_passes_real_video_artifacts():
    adapter = IntegrityQCAdapter(required_clip_count=2)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="/media/run/items/item-1/clip-0.mp4",
            meta={"provider": "replicate", "model": "lightricks/ltx-2.3-fast"},
        ),
        Artifact(
            kind="clip",
            uri="https://cdn.example.com/product-demo.webm",
            meta={"provider": "replicate", "model": "lightricks/ltx-2.3-fast"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.34)

    assert qc.passed is True
    assert qc.score == pytest.approx(1.0)
    assert qc.reasons == []


async def test_integrity_qc_rejects_mock_or_fallback_media():
    adapter = IntegrityQCAdapter(required_clip_count=2)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="mock://clip/item-1",
            meta={"provider": "mock", "tier": "kling"},
        ),
        Artifact(
            kind="clip",
            uri="/media/run/items/item-1/clip-1.mp4",
            meta={
                "provider": "mock",
                "fallback_reason": "replicate_model_not_configured",
            },
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is False
    assert qc.score == pytest.approx(0.0)
    assert "clip_0_mock_provider" in qc.reasons
    assert "clip_0_invalid_video_uri" in qc.reasons
    assert "clip_1_mock_provider" in qc.reasons
    assert "clip_1_fallback_reason:replicate_model_not_configured" in qc.reasons


async def test_integrity_qc_rejects_missing_required_clips():
    adapter = IntegrityQCAdapter(required_clip_count=2)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="/media/run/items/item-1/clip-0.mp4",
            meta={"provider": "replicate"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is False
    assert "missing_clips:1/2" in qc.reasons


async def test_integrity_qc_accepts_http_video_url_without_extension():
    """URLs de entrega do Replicate (replicate.delivery) muitas vezes não têm
    extensão no path — o clip é real e não pode ser reprovado por isso."""
    adapter = IntegrityQCAdapter(required_clip_count=1)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="https://replicate.delivery/pbxt/abc123/output",
            meta={"provider": "replicate", "model": "lightricks/ltx-2.3-fast"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is True
    assert qc.reasons == []


async def test_integrity_qc_accepts_video_url_with_query_string():
    adapter = IntegrityQCAdapter(required_clip_count=1)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="https://cdn.example.com/output.mp4?token=abc",
            meta={"provider": "replicate"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is True


async def test_integrity_qc_rejects_http_url_with_non_video_extension():
    adapter = IntegrityQCAdapter(required_clip_count=1)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="https://cdn.example.com/image.jpg",
            meta={"provider": "replicate"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is False
    assert "clip_0_invalid_video_uri" in qc.reasons


async def test_integrity_qc_accepts_data_video_uri():
    adapter = IntegrityQCAdapter(required_clip_count=1)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="data:video/mp4;base64,AAAA",
            meta={"provider": "vercel"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is True


async def test_integrity_qc_ignores_superseded_take_metadata():
    """A proveniência das takes descartadas (D33) é metadado, não um clip.

    ``meta["superseded_takes"]`` cita uris mock de takes rejeitadas; isso não pode
    reprovar o item — só os clips de fato anexados contam.
    """
    adapter = IntegrityQCAdapter(required_clip_count=2)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="/media/run/items/item-1/clip-0.mp4",
            meta={"provider": "replicate", "model": "lightricks/ltx-2.3-fast"},
        ),
        Artifact(
            kind="clip",
            uri="https://cdn.example.com/product-demo.webm",
            meta={
                "provider": "replicate",
                "model": "lightricks/ltx-2.3-fast",
                # Proveniência do agent: uma take paga e descartada, com provider mock.
                "agent_takes": 2,
                "superseded_takes": [
                    {"uri": "mock://clip-rejected", "cost_usd": 0.08, "revision": None}
                ],
            },
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.34)

    assert qc.passed is True, qc.reasons
    assert qc.reasons == []
