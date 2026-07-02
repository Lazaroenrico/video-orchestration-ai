"""Interfaces (Protocols) dos adapters de provedores.

No v1 só existe o ``MockAdapter``. Adapters reais (Claude, GPT Image 2, Topaz,
ElevenLabs, Replicate/fal/AtlasCloud) implementam estes mesmos protocolos e são
plugados via ``registry.py`` — sem mexer no grafo.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from orchestrator.graph.state import Artifact, JudgeVerdict, QCResult

VoicePreset = Literal["male", "female", "neutral"]

_FEMALE_HINTS = ("female", "feminina", "woman", "mulher", "girl")
_MALE_HINTS = ("male", "masculino", "masculina", "man", "homem", "boy")


@dataclass(frozen=True)
class VoiceProfile:
    """Perfil leve de voz para criação/reroll de creators."""

    preset: VoicePreset = "neutral"
    prompt: str = ""

    def __post_init__(self) -> None:
        prompt = self.prompt.strip()
        if self.preset not in ("male", "female", "neutral"):
            raise ValueError(f"unsupported voice preset: {self.preset}")
        object.__setattr__(self, "prompt", prompt)

    def as_dict(self) -> dict[str, str]:
        return {"preset": self.preset, "prompt": self.prompt}


def infer_voice_profile(text: Optional[str]) -> Optional[VoiceProfile]:
    """Infere preset de voz a partir de um briefing humano opcional."""
    prompt = (text or "").strip()
    if not prompt:
        return None
    lowered = prompt.casefold()
    if any(token in lowered for token in _FEMALE_HINTS):
        preset: VoicePreset = "female"
    elif any(token in lowered for token in _MALE_HINTS):
        preset = "male"
    else:
        preset = "neutral"
    return VoiceProfile(preset=preset, prompt=prompt)


def resolve_voice_profile(
    system_prompt: Optional[str],
    voice_profile: Optional[VoiceProfile] = None,
) -> Optional[VoiceProfile]:
    """Override explícito vence; senão, tenta inferir do texto existente."""
    if voice_profile is not None:
        return voice_profile
    return infer_voice_profile(system_prompt)


def assign_voice_profile(
    system_prompt: Optional[str],
    voice_profile: Optional[VoiceProfile] = None,
    *,
    index: int,
) -> VoiceProfile:
    """Perfil de voz **concreto** (nunca ``None``) para um creator do roster.

    Precedência: override explícito → gênero inferido do texto → gênero concreto
    determinístico por índice (alterna ``female``/``male``). Isso garante paridade
    imagem↔voz: o mesmo preset alimenta o prompt de imagem e a criação de voz, e o
    roster ganha variedade mesmo quando o briefing não cita gênero.
    """
    if voice_profile is not None:
        return voice_profile
    inferred = infer_voice_profile(system_prompt)
    if inferred is not None and inferred.preset != "neutral":
        return inferred
    prompt = inferred.prompt if inferred is not None else (system_prompt or "").strip()
    preset: VoicePreset = "female" if index % 2 == 0 else "male"
    return VoiceProfile(preset=preset, prompt=prompt)


_GENDER_CLAUSE: dict[VoicePreset, str] = {
    "female": "The creator is an adult woman.",
    "male": "The creator is an adult man.",
}


def image_gender_clause(profile: Optional[VoiceProfile]) -> str:
    """Frase brand-safe de gênero para o prompt de imagem (``""`` p/ neutral/None)."""
    if profile is None:
        return ""
    return _GENDER_CLAUSE.get(profile.preset, "")


@runtime_checkable
class LLMPort(Protocol):
    """Claude — conceitos (Step 1), scripts (Step 2)."""

    async def generate_concepts(
        self, offer: str, n: int, seed: str, bias: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        """``bias`` = hooks vencedores do ciclo anterior (Step 10 -> 1), opcional."""
        ...
    async def write_script(self, concept: dict[str, Any], creator_ref: str, platform: str) -> str: ...


@runtime_checkable
class CreatorPort(Protocol):
    """GPT Image 2 + Topaz + ElevenLabs — creator reutilizável (Step 3)."""

    async def build_creator(
        self,
        index: int,
        system_prompt: Optional[str] = None,
        voice_profile: Optional[VoiceProfile] = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class VoicePort(Protocol):
    """Sub-adapter de voz do creator."""

    async def create_voice(
        self, index: int, voice_profile: Optional[VoiceProfile] = None
    ) -> str: ...


@runtime_checkable
class VideoPort(Protocol):
    """LTX / Kling / Seedance via plataforma de geração (Steps 4 e 5)."""

    async def generate_clip(
        self, item_id: str, tier: str, seconds: int, attempt: int,
        system_prompt: Optional[str] = None,
        reference_image_uri: Optional[str] = None,
    ) -> Artifact: ...


@runtime_checkable
class QCPort(Protocol):
    """QC sistematizado (Step 7)."""

    async def qc_check(self, item_id: str, attempt: int, fail_rate: float) -> QCResult: ...


@runtime_checkable
class AssemblyPort(Protocol):
    """Montagem/edição (Step 8)."""

    async def assemble(self, item_id: str, platform: str) -> Artifact: ...


@runtime_checkable
class DistributionPort(Protocol):
    """Distribuição no portfolio de contas (Step 9)."""

    async def distribute(self, item_id: str) -> dict[str, Any]: ...


@runtime_checkable
class JudgePort(Protocol):
    """LLM Judge via API Gateway (avaliação determinística do QC)."""

    def judge(self, criteria: dict[str, Any], subject: dict[str, Any]) -> JudgeVerdict: ...
