"""TDD — system prompts opcionais (creator_prompt + video_prompt) (Seção A do plano).

- build_creator(0)  == legado (sem system_prompt)
- build_creator(0, system_prompt="x") ≠ legado, mas determinístico entre si
- generate_clip idem para URIs
- node_roster passa creator_prompt; make_gen_node/node_product_demo passam video_prompt
"""
from __future__ import annotations

import asyncio
import pytest

from orchestrator.adapters.mock import MockAdapter
from tests.conftest import TIERS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter():
    return MockAdapter(tiers=TIERS)


# ---------------------------------------------------------------------------
# A.2 MockAdapter — build_creator com system_prompt
# ---------------------------------------------------------------------------


async def test_build_creator_no_prompt_is_legacy(adapter):
    """Sem system_prompt o retorno deve ser idêntico ao original (mesmos campos e determinismo)."""
    c = await adapter.build_creator(0)
    c2 = await adapter.build_creator(0)
    assert c["id"] == "creator-0"
    assert c["upscaled_base"].startswith("data:image/svg+xml;base64,")
    assert c["upscaled_base"] == c2["upscaled_base"]  # determinístico
    assert c["voice_id"] == "voice-0"
    assert c["voice_preview_uri"].startswith("data:audio/wav;base64,")
    assert c["voice_preview_uri"] == c2["voice_preview_uri"]  # determinístico


async def test_build_creator_with_prompt_differs_from_legacy(adapter):
    """Com system_prompt a URI muda (sufixo hash)."""
    c_plain = await adapter.build_creator(0)
    c_prompt = await adapter.build_creator(0, system_prompt="portrait artist style")
    assert c_prompt["upscaled_base"] != c_plain["upscaled_base"]
    assert c_prompt["voice_id"] != c_plain["voice_id"]


async def test_build_creator_with_prompt_is_deterministic(adapter):
    """Duas chamadas com o mesmo prompt retornam o mesmo resultado."""
    p = "portrait artist style"
    c1 = await adapter.build_creator(0, system_prompt=p)
    c2 = await adapter.build_creator(0, system_prompt=p)
    assert c1 == c2


async def test_build_creator_different_prompts_differ(adapter):
    c1 = await adapter.build_creator(0, system_prompt="style A")
    c2 = await adapter.build_creator(0, system_prompt="style B")
    assert c1["upscaled_base"] != c2["upscaled_base"]


# ---------------------------------------------------------------------------
# A.2 MockAdapter — generate_clip com system_prompt
# ---------------------------------------------------------------------------


async def test_generate_clip_no_prompt_is_legacy(adapter):
    art = await adapter.generate_clip("item-1", "ltx", 8, 1)
    art2 = await adapter.generate_clip("item-1", "ltx", 8, 1)
    assert art.uri.startswith("data:video/mp4;base64,")
    assert art.uri == art2.uri  # determinístico


async def test_generate_clip_with_prompt_uri_differs(adapter):
    art_plain = await adapter.generate_clip("item-1", "ltx", 8, 1)
    art_prompt = await adapter.generate_clip("item-1", "ltx", 8, 1, system_prompt="dramatic")
    assert art_prompt.uri != art_plain.uri


async def test_generate_clip_with_prompt_is_deterministic(adapter):
    p = "dramatic"
    a1 = await adapter.generate_clip("item-1", "ltx", 8, 1, system_prompt=p)
    a2 = await adapter.generate_clip("item-1", "ltx", 8, 1, system_prompt=p)
    assert a1.uri == a2.uri


async def test_generate_clip_cost_unchanged_with_prompt(adapter):
    """O custo não deve mudar com system_prompt."""
    a_plain = await adapter.generate_clip("item-1", "ltx", 8, 1)
    a_prompt = await adapter.generate_clip("item-1", "ltx", 8, 1, system_prompt="x")
    assert a_prompt.meta["cost_usd"] == a_plain.meta["cost_usd"]


# ---------------------------------------------------------------------------
# A.4 node_roster passa creator_prompt
# ---------------------------------------------------------------------------


async def test_node_roster_passes_creator_prompt(pipeline_cfg, adapter):
    """SpyAdapter verifica se build_creator recebeu system_prompt correto."""

    received: list = []

    class SpyAdapter(MockAdapter):
        async def build_creator(self, index: int, system_prompt=None):
            received.append(system_prompt)
            return await super().build_creator(index, system_prompt=system_prompt)

    spy = SpyAdapter(tiers=TIERS)

    from orchestrator.nodes.stages import node_roster

    config = {
        "configurable": {
            "adapter": spy,
            "pipeline": pipeline_cfg,
            "run": {"creator_prompt": "TEST_CREATOR_PROMPT"},
        }
    }
    state = {}
    await node_roster(state, config)

    assert all(p == "TEST_CREATOR_PROMPT" for p in received), f"received: {received}"


async def test_node_roster_no_prompt_passes_none(pipeline_cfg, adapter):
    """Sem creator_prompt no run, build_creator recebe system_prompt=None."""

    received: list = []

    class SpyAdapter(MockAdapter):
        async def build_creator(self, index: int, system_prompt=None):
            received.append(system_prompt)
            return await super().build_creator(index)

    spy = SpyAdapter(tiers=TIERS)

    from orchestrator.nodes.stages import node_roster

    config = {
        "configurable": {
            "adapter": spy,
            "pipeline": pipeline_cfg,
            "run": {},
        }
    }
    await node_roster({}, config)
    assert all(p is None for p in received)


# ---------------------------------------------------------------------------
# A.4 make_gen_node / node_product_demo passam video_prompt
# ---------------------------------------------------------------------------


async def test_gen_node_passes_video_prompt(pipeline_cfg, adapter):
    received: list[dict] = []

    class SpyAdapter(MockAdapter):
        async def generate_clip(
            self,
            item_id,
            tier,
            seconds,
            attempt,
            system_prompt=None,
            reference_image_uri=None,
        ):
            received.append(
                {
                    "system_prompt": system_prompt,
                    "reference_image_uri": reference_image_uri,
                }
            )
            return await super().generate_clip(
                item_id,
                tier,
                seconds,
                attempt,
                system_prompt=system_prompt,
                reference_image_uri=reference_image_uri,
            )

    spy = SpyAdapter(tiers=TIERS)

    from orchestrator.nodes.stages import make_gen_node
    from orchestrator.graph.state import Item

    gen = make_gen_node("ltx")
    item = Item(
        concept={"id": "c1", "hook": "Hook A", "hook_style": "problem", "offer": "serum"},
        script="HOOK: Hook A\nCTA: test hoje.",
        creator_image_uri="data:image/png;base64,abc",
    )
    config = {
        "configurable": {
            "adapter": spy,
            "pipeline": pipeline_cfg,
            "run": {"video_prompt": "VIDEO_PROMPT"},
        }
    }
    await gen(item.model_dump(), config)
    assert received[0]["reference_image_uri"] == "data:image/png;base64,abc"
    assert "VIDEO_PROMPT" in received[0]["system_prompt"]
    assert "HOOK: Hook A" in received[0]["system_prompt"]
    assert "talking-head" in received[0]["system_prompt"]


async def test_node_product_demo_passes_video_prompt(pipeline_cfg, adapter):
    received: list[dict] = []

    class SpyAdapter(MockAdapter):
        async def generate_clip(
            self,
            item_id,
            tier,
            seconds,
            attempt,
            system_prompt=None,
            reference_image_uri=None,
        ):
            received.append(
                {
                    "system_prompt": system_prompt,
                    "reference_image_uri": reference_image_uri,
                }
            )
            return await super().generate_clip(
                item_id,
                tier,
                seconds,
                attempt,
                system_prompt=system_prompt,
                reference_image_uri=reference_image_uri,
            )

    spy = SpyAdapter(tiers=TIERS)

    from orchestrator.nodes.stages import node_product_demo
    from orchestrator.graph.state import Item

    item = Item(
        concept={"id": "c1", "offer": "serum"},
        script="CTA: compra hoje.",
        creator_image_uri="data:image/png;base64,abc",
    )
    config = {
        "configurable": {
            "adapter": spy,
            "pipeline": pipeline_cfg,
            "run": {"video_prompt": "MY_VIDEO_PROMPT"},
        }
    }
    await node_product_demo(item.model_dump(), config)
    assert received[0]["reference_image_uri"] == "data:image/png;base64,abc"
    assert "MY_VIDEO_PROMPT" in received[0]["system_prompt"]
    assert "CTA: compra hoje." in received[0]["system_prompt"]
    assert "product-demo" in received[0]["system_prompt"]
