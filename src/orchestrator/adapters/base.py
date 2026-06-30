"""Interfaces (Protocols) dos adapters de provedores.

No v1 só existe o ``MockAdapter``. Adapters reais (Claude, GPT Image 2, Topaz,
ElevenLabs, Replicate/fal/AtlasCloud) implementam estes mesmos protocolos e são
plugados via ``registry.py`` — sem mexer no grafo.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from orchestrator.graph.state import Artifact, JudgeVerdict, QCResult


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

    async def build_creator(self, index: int, system_prompt: Optional[str] = None) -> dict[str, Any]: ...


@runtime_checkable
class VideoPort(Protocol):
    """LTX / Kling / Seedance via plataforma de geração (Steps 4 e 5)."""

    async def generate_clip(
        self, item_id: str, tier: str, seconds: int, attempt: int,
        system_prompt: Optional[str] = None,
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
