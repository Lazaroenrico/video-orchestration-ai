"""MockAdapter — implementação dry-run de todos os ports.

Saídas **determinísticas** (derivadas de hash dos inputs, sem ``random``) para que
toda a pipeline rode ponta a ponta sem rede e os testes sejam reproduzíveis.
Custo por tier segue o Context.md (LTX $0.01/s, Kling $0.10/s, Seedance $0.168/s).
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Optional

from orchestrator.graph.state import Artifact, QCResult
from orchestrator.tracing import traced

_HOOK_STYLES = ["problem", "curiosity", "bold_claim", "emotional", "social_proof"]
_QC_SUSPECTS = ["hands", "eyes", "lip_sync", "lighting", "skin_texture"]


def _unit(*parts: Any) -> float:
    """Hash determinístico dos inputs -> float uniforme em [0, 1)."""
    key = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(key.encode()).hexdigest()
    return int(digest[:12], 16) / float(1 << 48)


class MockAdapter:
    """Serve a todos os papéis (llm/image/voice/video/assembly/distribution) no v1."""

    def __init__(self, tiers: list[dict[str, Any]], latency: float = 0.0) -> None:
        self.tiers = {t["name"]: t for t in tiers}
        self.latency = latency
        self._semaphores = {
            name: asyncio.Semaphore(int(t.get("max_concurrency", 8)))
            for name, t in self.tiers.items()
        }

    async def _tick(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)

    # --- Step 1: conceitos ---
    @traced("adapter.mock.generate_concepts", run_type="chain", step=1, provider="mock")
    async def generate_concepts(
        self,
        offer: str,
        n: int,
        seed: str,
        bias: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        await self._tick()
        # bias = hooks vencedores do ciclo anterior (Step 10 -> 1). Uma fração dos
        # conceitos é puxada para esses estilos, mantendo determinismo e spread.
        bias = [b for b in (bias or []) if b in _HOOK_STYLES]
        bias_strength = 0.6
        concepts: list[dict[str, Any]] = []
        for i in range(n):
            style = _HOOK_STYLES[int(_unit(seed, offer, i) * len(_HOOK_STYLES))]
            if bias and _unit("bias", seed, offer, i) < bias_strength:
                style = bias[i % len(bias)]
            tag = hashlib.sha256(f"{seed}|{offer}|{i}".encode()).hexdigest()[:8]
            concepts.append(
                {
                    "id": f"concept-{tag}",
                    "offer": offer,
                    "hook": f"hook[{style}]-{tag}",
                    "angle": style,
                    "hook_style": style,
                    "format": ["talking_head", "demo", "reaction"][i % 3],
                }
            )
        return concepts

    # --- Step 2: scripts ---
    @traced("adapter.mock.write_script", run_type="chain", step=2, provider="mock")
    async def write_script(self, concept: dict[str, Any], creator_ref: str, platform: str) -> str:
        await self._tick()
        hook = concept.get("hook", "hook")
        pacing = "fast" if platform.lower() == "tiktok" else "medium"
        return (
            f"HOOK: {hook}\n"
            f"BODY: ({platform} / pacing={pacing}) creator={creator_ref} fala sobre "
            f"{concept.get('offer', 'o produto')} no ângulo {concept.get('angle')}.\n"
            f"CTA: confere o link e testa hoje."
        )

    # --- Step 3: creator reutilizável ---
    @traced("adapter.mock.build_creator", run_type="tool", step=3, provider="mock")
    async def build_creator(self, index: int, system_prompt: Optional[str] = None) -> dict[str, Any]:
        await self._tick()
        sfx = ""
        if system_prompt:
            sfx = "-" + hashlib.sha256(system_prompt.encode()).hexdigest()[:8]
        return {
            "id": f"creator-{index}",
            "angles": ["front", "3/4", "profile", "smile", "neutral"],
            "upscaled_base": f"mock://creator/{index}{sfx}/base_4k.png",
            "voice_id": f"voice-{index}{sfx}",
        }

    # --- Steps 4/5: vídeo (talking-head / demo) ---
    @traced("adapter.mock.generate_clip", run_type="tool", step="video", provider="mock")
    async def generate_clip(
        self, item_id: str, tier: str, seconds: int, attempt: int,
        system_prompt: Optional[str] = None,
    ) -> Artifact:
        spec = self.tiers[tier]  # KeyError em tier desconhecido (contratual)
        async with self._semaphores[tier]:
            await self._tick()
            cost = round(spec["cost_per_second"] * seconds, 4)
            sfx = ""
            if system_prompt:
                sfx = "-" + hashlib.sha256(system_prompt.encode()).hexdigest()[:8]
            meta: dict[str, Any] = {
                "tier": tier,
                "model": spec["model"],
                "seconds": seconds,
                "cost_usd": cost,
                "attempt": attempt,
            }
            if system_prompt:
                meta["system_prompt"] = system_prompt
            return Artifact(
                kind="clip",
                uri=f"mock://clip/{item_id}/a{attempt}{sfx}",
                meta=meta,
            )

    # --- Step 7: QC ---
    @traced("adapter.mock.qc_check", run_type="tool", step=7, provider="mock")
    async def qc_check(self, item_id: str, attempt: int, fail_rate: float) -> QCResult:
        await self._tick()
        base = _unit("qc", item_id)
        score = min(0.999, base + 0.25 * attempt)
        passed = score >= fail_rate
        reasons: list[str] = []
        if not passed:
            k = 1 + int(_unit("nreasons", item_id) * 2)  # 1..2 problemas
            start = int(_unit("which", item_id) * len(_QC_SUSPECTS))
            reasons = [_QC_SUSPECTS[(start + j) % len(_QC_SUSPECTS)] for j in range(k)]
        return QCResult(passed=passed, score=round(score, 4), reasons=reasons)

    # --- Step 8: montagem ---
    @traced("adapter.mock.assemble", run_type="tool", step=8, provider="mock")
    async def assemble(self, item_id: str, platform: str) -> Artifact:
        await self._tick()
        return Artifact(
            kind="video",
            uri=f"mock://video/{item_id}.mp4",
            meta={"captions": True, "broll": True, "platform": platform},
        )

    # --- Step 9: distribuição ---
    @traced("adapter.mock.distribute", run_type="tool", step=9, provider="mock")
    async def distribute(self, item_id: str) -> dict[str, Any]:
        await self._tick()
        acct = int(_unit("acct", item_id) * 20)
        hour = int(_unit("hour", item_id) * 24)
        return {
            "account": f"acct-{acct:02d}",
            "scheduled_at": f"2026-06-27T{hour:02d}:00:00",
            "status": "scheduled",
        }


def build_mock_adapter(tiers: list[dict[str, Any]], latency: Optional[float] = None) -> MockAdapter:
    return MockAdapter(tiers=tiers, latency=latency or 0.0)
