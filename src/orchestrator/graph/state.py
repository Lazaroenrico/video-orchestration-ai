"""Schemas de estado do grafo da pipeline de AI UGC.

- ``Item``: registro por-item que flui pelo subgrafo per-item (conceito -> script ->
  clips -> qc -> assembled).
- ``BatchState``: estado do grafo de topo; usa reducers aditivos nas chaves acumuladas
  em paralelo durante o fan-out (``results``, ``total_cost_usd``).
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class Artifact(BaseModel):
    """Um artefato produzido por um stage (clip, voz, imagem, vídeo montado...)."""

    kind: str
    uri: str
    meta: dict[str, Any] = Field(default_factory=dict)


class QCResult(BaseModel):
    """Resultado do QC (Step 7) para um item."""

    passed: bool
    score: float
    reasons: list[str] = Field(default_factory=list)


class JudgeVerdict(BaseModel):
    """Veredito do LLM Judge (via API Gateway)."""

    score: float
    verdict: str
    passed: bool
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_response(
        cls,
        score: float,
        verdict: Optional[str],
        threshold: float,
        raw: Optional[dict[str, Any]] = None,
    ) -> "JudgeVerdict":
        """Constrói o veredito a partir da resposta do gateway.

        Se o gateway devolve um ``verdict`` explícito ("pass"/"fail"), ele manda;
        caso contrário deriva-se de ``score >= threshold``.
        """
        if verdict is not None:
            passed = verdict.strip().lower() in {"pass", "passed", "ok", "true", "1"}
        else:
            passed = score >= threshold
            verdict = "pass" if passed else "fail"
        return cls(score=score, verdict=verdict, passed=passed, raw=raw or {})


class Item(BaseModel):
    """Estado per-item que flui pelo subgrafo de produção."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    concept: dict[str, Any]
    creator_ref: Optional[str] = None
    creator_image_uri: Optional[str] = None
    creator_image_local_path: Optional[str] = None
    script: Optional[str] = None
    tier: Optional[str] = None
    clips: list[Artifact] = Field(default_factory=list)
    qc: Optional[QCResult] = None
    attempts: int = 0
    assembled: Optional[Artifact] = None
    dropped: bool = False
    error: Optional[str] = None
    cost_usd: float = 0.0


def new_item(
    concept: dict[str, Any],
    creator_ref: Optional[str] = None,
    creator_image_uri: Optional[str] = None,
    creator_image_local_path: Optional[str] = None,
) -> Item:
    """Factory de um novo ``Item`` a partir de um conceito."""
    return Item(
        concept=concept,
        creator_ref=creator_ref,
        creator_image_uri=creator_image_uri,
        creator_image_local_path=creator_image_local_path,
    )


def add_items(left: Optional[list[Item]], right: Optional[list[Item]]) -> list[Item]:
    """Reducer aditivo para acumular itens vindos do fan-out paralelo."""
    return (left or []) + (right or [])


def add_cost(left: Optional[float], right: Optional[float]) -> float:
    """Reducer aditivo para o custo total acumulado."""
    return (left or 0.0) + (right or 0.0)


class BatchState(TypedDict, total=False):
    """Estado do grafo de topo (um batch/semana de produção)."""

    run_id: str
    concepts: list[dict[str, Any]]
    roster: list[dict[str, Any]]
    results: Annotated[list[Item], add_items]
    total_cost_usd: Annotated[float, add_cost]
    config: dict[str, Any]
    feedback: dict[str, Any]
