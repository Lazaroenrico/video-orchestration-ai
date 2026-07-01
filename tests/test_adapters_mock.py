"""Testes do MockAdapter — fixtures determinísticos, custo por tier, QC determinístico."""
import pytest

from orchestrator.adapters.mock import MockAdapter
from orchestrator.graph.state import Artifact, QCResult
from orchestrator.web.server import _normalize_artifact, _normalize_creator

TIERS = [
    {"name": "ltx", "model": "ltx-2.3", "cost_per_second": 0.01, "max_concurrency": 16},
    {"name": "kling", "model": "kling-3.0", "cost_per_second": 0.10, "max_concurrency": 6},
    {"name": "seedance", "model": "seedance-2.0", "cost_per_second": 0.168, "max_concurrency": 2},
]


@pytest.fixture
def adapter():
    return MockAdapter(tiers=TIERS)


# --- concepts (Step 1) ---

async def test_generate_concepts_count_and_determinism(adapter):
    a = await adapter.generate_concepts(offer="serum X", n=10, seed="wk1")
    b = await adapter.generate_concepts(offer="serum X", n=10, seed="wk1")
    assert len(a) == 10
    assert a == b  # determinístico
    # spread: mais de um estilo de hook no batch (não 10 cópias da mesma ideia)
    assert len({c["hook_style"] for c in a}) > 1


async def test_generate_concepts_seed_changes_output(adapter):
    a = await adapter.generate_concepts(offer="serum X", n=5, seed="wk1")
    b = await adapter.generate_concepts(offer="serum X", n=5, seed="wk2")
    assert a != b


# --- scripts (Step 2) ---

async def test_write_script_has_hook_and_cta(adapter):
    concept = {"hook": "você está fazendo errado", "angle": "problema", "hook_style": "problem"}
    script = await adapter.write_script(concept, creator_ref="creator-1", platform="tiktok")
    assert isinstance(script, str)
    assert "HOOK" in script.upper()
    assert "CTA" in script.upper()
    assert "tiktok" in script.lower()  # calibrado por plataforma


# --- creator/roster (Step 3) ---

async def test_build_creator_locks_identity(adapter):
    creator = await adapter.build_creator(index=0)
    assert creator["id"]
    assert len(creator["angles"]) >= 3       # front, 3/4, profile (multi-ângulo)
    assert creator["upscaled_base"].startswith("data:image/svg+xml;base64,")  # renderável offline
    assert creator["voice_id"]                # voz ElevenLabs consistente
    assert creator["voice_preview_uri"].startswith("data:audio/wav;base64,")  # preview renderável


async def test_build_creator_media_is_renderable_in_ui(adapter):
    """Regressão do WS-2: o creator mock deve chegar à UI marcado como renderável."""
    creator = await adapter.build_creator(index=0)
    norm = _normalize_creator(creator)
    assert norm["image_uri"] == creator["upscaled_base"]
    art = _normalize_artifact({"kind": "face", "uri": creator["upscaled_base"]})
    assert art["media_type"] == "image"
    assert art["renderable"] is True
    voice_art = _normalize_artifact({"kind": "voice_preview", "uri": creator["voice_preview_uri"]})
    assert voice_art["media_type"] == "audio"
    assert voice_art["renderable"] is True


async def test_build_creator_is_deterministic(adapter):
    a = await adapter.build_creator(index=0)
    b = await adapter.build_creator(index=0)
    assert a == b
    c = await adapter.build_creator(index=1)
    assert c["upscaled_base"] != a["upscaled_base"]
    assert c["voice_preview_uri"] != a["voice_preview_uri"]


# --- video (Steps 4/5): custo por tier ---

async def test_generate_clip_cost_matches_tier(adapter):
    clip = await adapter.generate_clip(item_id="i1", tier="ltx", seconds=8, attempt=0)
    assert isinstance(clip, Artifact)
    assert clip.kind == "clip"
    assert clip.meta["tier"] == "ltx"
    assert clip.meta["cost_usd"] == pytest.approx(0.01 * 8)
    seed = await adapter.generate_clip(item_id="i1", tier="seedance", seconds=8, attempt=0)
    assert seed.meta["cost_usd"] == pytest.approx(0.168 * 8)


async def test_generate_clip_unknown_tier_raises(adapter):
    with pytest.raises(KeyError):
        await adapter.generate_clip(item_id="i1", tier="nope", seconds=8, attempt=0)


async def test_generate_clip_deterministic_uri(adapter):
    a = await adapter.generate_clip(item_id="i1", tier="ltx", seconds=8, attempt=1)
    b = await adapter.generate_clip(item_id="i1", tier="ltx", seconds=8, attempt=1)
    assert a.uri == b.uri
    assert a.uri.startswith("data:video/mp4;base64,")


async def test_generate_clip_media_is_renderable_in_ui(adapter):
    """Regressão do WS-2: o clip mock deve chegar à UI marcado como renderável."""
    clip = await adapter.generate_clip(item_id="i1", tier="ltx", seconds=8, attempt=0)
    art = _normalize_artifact({"kind": clip.kind, "uri": clip.uri})
    assert art["media_type"] == "video"
    assert art["renderable"] is True


# --- QC (Step 7): determinístico, fração reprovada, melhora com tentativas ---

async def test_qc_check_is_deterministic(adapter):
    a = await adapter.qc_check(item_id="i1", attempt=0, fail_rate=0.34)
    b = await adapter.qc_check(item_id="i1", attempt=0, fail_rate=0.34)
    assert isinstance(a, QCResult)
    assert a == b


async def test_qc_fail_fraction_matches_rate(adapter):
    n = 400
    fails = 0
    for i in range(n):
        r = await adapter.qc_check(item_id=f"id-{i}", attempt=0, fail_rate=0.34)
        fails += 0 if r.passed else 1
    frac = fails / n
    assert 0.27 < frac < 0.41  # ~0.34 com tolerância estatística


async def test_qc_improves_with_attempts(adapter):
    # Um item que reprova no attempt 0 deve eventualmente passar com mais tentativas.
    failing = None
    for i in range(100):
        r = await adapter.qc_check(item_id=f"x-{i}", attempt=0, fail_rate=0.34)
        if not r.passed:
            failing = f"x-{i}"
            break
    assert failing is not None
    s0 = (await adapter.qc_check(item_id=failing, attempt=0, fail_rate=0.34)).score
    s2 = (await adapter.qc_check(item_id=failing, attempt=2, fail_rate=0.34)).score
    assert s2 >= s0
    assert (await adapter.qc_check(item_id=failing, attempt=2, fail_rate=0.34)).passed


async def test_qc_failure_has_reasons(adapter):
    for i in range(100):
        r = await adapter.qc_check(item_id=f"r-{i}", attempt=0, fail_rate=0.34)
        if not r.passed:
            assert r.reasons  # "usual suspects": hands/eyes/lips/lighting
            break
    else:
        pytest.fail("nenhuma reprovação encontrada para checar reasons")


# --- assembly (Step 8) / distribution (Step 9) ---

async def test_assemble_returns_video_artifact(adapter):
    art = await adapter.assemble(item_id="i1", platform="tiktok")
    assert art.kind == "video"
    assert art.meta["captions"] is True
    assert art.uri.startswith("data:video/mp4;base64,")
    norm = _normalize_artifact({"kind": art.kind, "uri": art.uri})
    assert norm["media_type"] == "video"
    assert norm["renderable"] is True


async def test_distribute_returns_schedule(adapter):
    res = await adapter.distribute(item_id="i1")
    assert res["account"]
    assert res["scheduled_at"]
