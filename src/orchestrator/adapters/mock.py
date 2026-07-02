"""MockAdapter — implementação dry-run de todos os ports.

Saídas **determinísticas** (derivadas de hash dos inputs, sem ``random``) para que
toda a pipeline rode ponta a ponta sem rede e os testes sejam reproduzíveis.
Custo por tier segue o Context.md (LTX $0.01/s, Kling $0.10/s, Seedance $0.168/s).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any, Optional

from orchestrator.adapters.base import VoiceProfile, resolve_voice_profile
from orchestrator.graph.state import Artifact, QCResult
from orchestrator.tracing import traced

_HOOK_STYLES = ["problem", "curiosity", "bold_claim", "emotional", "social_proof"]
_QC_SUSPECTS = ["hands", "eyes", "lip_sync", "lighting", "skin_texture"]


def _unit(*parts: Any) -> float:
    """Hash determinístico dos inputs -> float uniforme em [0, 1)."""
    key = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(key.encode()).hexdigest()
    return int(digest[:12], 16) / float(1 << 48)


def _digest_bytes(*parts: Any) -> bytes:
    key = "|".join(str(p) for p in parts)
    return hashlib.sha256(key.encode()).digest()


def _svg_data_uri(label: str, *seed_parts: Any) -> str:
    """SVG minúsculo e determinístico, renderável como imagem (sem rede/disco).

    A cor é derivada de hash dos ``seed_parts`` e o texto do rótulo torna o
    payload legível/único por creator, mantendo tudo pequeno (poucas centenas
    de bytes) — importante porque estes data URIs trafegam pelo buffer de SSE.
    """
    color = "#%06x" % (int(_unit(*seed_parts) * 0xFFFFFF))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        f'<rect width="64" height="64" fill="{color}"/>'
        f'<text x="32" y="36" font-size="9" text-anchor="middle" fill="#fff">{label}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _wav_data_uri(*seed_parts: Any) -> str:
    """WAV PCM 8-bit mono minúsculo (~0.1s) e determinístico.

    Cabeçalho RIFF/WAVE válido + amostras derivadas de hash (sem ``random``).
    Curto o bastante para caber no buffer de replay do SSE.
    """
    sample_rate = 4000
    n_samples = 400  # ~0.1s @ 4kHz
    digest = _digest_bytes("voice-preview", *seed_parts)
    samples = bytes(digest[i % len(digest)] for i in range(n_samples))
    data_size = len(samples)
    byte_rate = sample_rate  # mono, 8 bits/sample
    header = (
        b"RIFF"
        + (36 + data_size).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # PCM
        + (1).to_bytes(2, "little")  # mono
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # block align
        + (8).to_bytes(2, "little")  # bits per sample
        + b"data"
        + data_size.to_bytes(4, "little")
    )
    return "data:audio/wav;base64," + base64.b64encode(header + samples).decode()


# mp4 H.264 minúsculo, VÁLIDO e REPRODUZÍVEL (1 frame azul 16x16, faststart:
# moov antes do mdat) — 932 bytes. Gerado offline uma vez; embutido como
# constante para que a UI toque o vídeo no demo sem rede/disco/custo.
_MP4_PLAYABLE_B64 = (
    "AAAAIGZ0eXBtcDQyAAAAAG1wNDJtcDQxaXNvbWlzbzIAAANHbW9vdgAAAGxtdmhkAAAAAOZrEP3maxD9"
    "AAAMgAAADIAAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAApZ0cmFrAAAAXHRraGQAAAAH5msQ/eZrEP0AAAAB"
    "AAAAAAAADIAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAA"
    "ABAAAAAQAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAyAAAAAAAABAAAAAAHRbWRpYQAAACBtZGhk"
    "AAAAAOZrEP3maxD9AAAAZAAAAGRVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRl"
    "b0hhbmRsZXIAAAABfG1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAA"
    "AQAAAAx1cmwgAAAAAQAAATxzdGJsAAAAwHN0c2QAAAAAAAAAAQAAALBhdmMxAAAAAAAAAAEAAAAAAAAA"
    "AAAAAAAAAAAAABAAEABIAAAASAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "GP//AAAAI2F2Y0MBQtAL/+EADGdC0AuMjU5APCIRqAEABGjOPIAAAAAUYnRydAAAAAAAAAAAAAABqAAA"
    "ABNjb2xybmNseAAGAAYABgAAAAAQcGFzcAAAAAEAAAABAAAAGHN0dHMAAAAAAAAAAQAAAAEAAABkAAAA"
    "FHN0c3MAAAAAAAAAAQAAAAEAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAEAAAABAAAAGHN0c3oAAAAAAAAA"
    "AAAAAAEAAAA1AAAAFHN0Y28AAAAAAAAAAQAAA28AAAA9dWR0YQAAADVtZXRhAAAAAAAAACFoZGxyAAAA"
    "AG1obHJtZGlyAAAAAAAAAAAAAAAAAAAAAAhpbHN0AAAAPXVkdGEAAAA1bWV0YQAAAAAAAAAhaGRscgAA"
    "AABtaGxybWRpcgAAAAAAAAAAAAAAAAAAAAAIaWxzdAAAAD1tZGF0AAAADGdC0AuMjU5APCIRqAAAAARo"
    "zjyAAAAAGWW4AAQAAAn///giigACBr44AAhfRwABADw="
)


def _mp4_data_uri(*seed_parts: Any) -> str:
    """Vídeo REPRODUZÍVEL e determinístico (``data:video/mp4;base64,...``).

    Retorna um mp4 H.264 minúsculo, válido e tocável (constante compartilhada)
    para que o player da UI funcione no demo offline. Um ``#fragment`` derivado
    de hash dos ``seed_parts`` é anexado: o navegador o descarta ao decodificar
    (o mp4 tocado é idêntico), mas a string da URI varia por item/prompt —
    preservando o contrato de que outputs diferentes têm URIs diferentes, sem
    quebrar a reprodução nem a classificação ``data:video/mp4``.
    """
    tag = hashlib.sha256("|".join(str(p) for p in seed_parts).encode()).hexdigest()[:8]
    return "data:video/mp4;base64," + _MP4_PLAYABLE_B64 + "#" + tag


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
    async def build_creator(
        self,
        index: int,
        system_prompt: Optional[str] = None,
        voice_profile: Optional[VoiceProfile] = None,
    ) -> dict[str, Any]:
        await self._tick()
        sfx = ""
        if system_prompt:
            sfx = "-" + hashlib.sha256(system_prompt.encode()).hexdigest()[:8]
        resolved_voice = resolve_voice_profile(system_prompt, voice_profile)
        voice_seed = sfx
        if resolved_voice is not None:
            voice_seed += "-" + hashlib.sha256(
                f"{resolved_voice.preset}|{resolved_voice.prompt}".encode()
            ).hexdigest()[:8]
        # A imagem também codifica o preset resolvido: mesmo sem rosto real, o mock
        # mantém paridade imagem↔voz em nível de metadado/determinismo.
        image_preset = resolved_voice.preset if resolved_voice is not None else ""
        creator = {
            "id": f"creator-{index}",
            "angles": ["front", "3/4", "profile", "smile", "neutral"],
            "upscaled_base": _svg_data_uri(
                f"C{index}{sfx}", "creator", index, sfx, image_preset
            ),
            "voice_id": f"voice-{index}{voice_seed}",
            "voice_preview_uri": _wav_data_uri(
                "creator",
                index,
                voice_seed,
                resolved_voice.preset if resolved_voice is not None else "",
                resolved_voice.prompt if resolved_voice is not None else "",
            ),
        }
        if resolved_voice is not None:
            creator["voice_profile"] = resolved_voice.as_dict()
        return creator

    # --- Steps 4/5: vídeo (talking-head / demo) ---
    @traced("adapter.mock.generate_clip", run_type="tool", step="video", provider="mock")
    async def generate_clip(
        self, item_id: str, tier: str, seconds: int, attempt: int,
        system_prompt: Optional[str] = None,
        reference_image_uri: Optional[str] = None,
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
            if reference_image_uri:
                meta["has_reference_image"] = True
            return Artifact(
                kind="clip",
                uri=_mp4_data_uri("clip", item_id, attempt, sfx),
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
            uri=_mp4_data_uri("video", item_id),
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
