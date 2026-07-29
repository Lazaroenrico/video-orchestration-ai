"""Integrity QC for live media artifacts."""
from __future__ import annotations

import pytest

from orchestrator.adapters.integrity_qc import IntegrityQCAdapter
from orchestrator.graph.state import Artifact, Item
from orchestrator.media_store import persist_item_media
from orchestrator.storage.r2 import R2MediaStorage


def _item_with_clips(*clips: Artifact) -> Item:
    return Item(
        id="item-1",
        concept={"hook": "before-after"},
        script="HOOK: test\nCTA: buy now",
        clips=list(clips),
    )


class _FakeObjectStorageClient:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803
        self.objects[Key] = {
            "bucket": Bucket,
            "body": Body,
            "content_type": ContentType,
        }


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


async def test_integrity_qc_accepts_canonical_r2_video_pointer():
    adapter = IntegrityQCAdapter(required_clip_count=1)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="r2://generation-video/run-1/items/item-1/clip-0.mp4",
            meta={"provider": "replicate", "storage_backend": "r2"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is True
    assert qc.reasons == []


async def test_integrity_qc_accepts_clip_after_r2_persistence():
    client = _FakeObjectStorageClient()
    storage = R2MediaStorage(bucket="generation-video", client=client)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="data:video/mp4;base64,AAAA",
            meta={"provider": "replicate"},
        ),
    )

    persisted = await persist_item_media(item, run_id="run-1", storage=storage)
    qc = await IntegrityQCAdapter(required_clip_count=1).qc_check(
        item=persisted,
        fail_rate=0.0,
    )

    assert persisted.clips[0].uri == (
        "r2://generation-video/run-1/items/item-1/clip-0.mp4"
    )
    assert persisted.clips[0].meta["source_uri"] == "data:video/mp4;base64,AAAA"
    assert persisted.clips[0].meta["storage_backend"] == "r2"
    assert "run-1/items/item-1/clip-0.mp4" in client.objects
    assert qc.passed is True
    assert qc.reasons == []


async def test_integrity_qc_accepts_canonical_s3_video_pointer():
    adapter = IntegrityQCAdapter(required_clip_count=1)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="s3://ugc-aws/run-1/items/item-1/clip-0.webm",
            meta={"provider": "replicate", "storage_backend": "s3"},
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is True
    assert qc.reasons == []


@pytest.mark.parametrize(
    "uri",
    [
        "r2://generation-video/run-1/items/item-1/image.jpg",
        "s3://ugc-aws/run-1/items/item-1/object-without-extension",
        "r2:///run-1/items/item-1/clip-0.mp4",
        "s3://ugc-aws/",
    ],
)
async def test_integrity_qc_rejects_invalid_canonical_storage_pointer(uri):
    adapter = IntegrityQCAdapter(required_clip_count=1)
    item = _item_with_clips(
        Artifact(kind="clip", uri=uri, meta={"provider": "replicate"}),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is False
    assert qc.reasons == ["clip_0_invalid_video_uri"]


async def test_integrity_qc_still_rejects_mock_or_fallback_canonical_media():
    adapter = IntegrityQCAdapter(required_clip_count=2)
    item = _item_with_clips(
        Artifact(
            kind="clip",
            uri="r2://generation-video/run-1/items/item-1/clip-0.mp4",
            meta={"provider": "mock"},
        ),
        Artifact(
            kind="clip",
            uri="r2://generation-video/run-1/items/item-1/clip-1.mp4",
            meta={
                "provider": "replicate",
                "fallback_reason": "provider_output_unavailable",
            },
        ),
    )

    qc = await adapter.qc_check(item=item, fail_rate=0.0)

    assert qc.passed is False
    assert qc.reasons == [
        "clip_0_mock_provider",
        "clip_1_fallback_reason:provider_output_unavailable",
    ]


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
