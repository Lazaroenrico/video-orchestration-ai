"""Módulo de avaliação determinística e LLM-as-judge (LangSmith-style)."""
from __future__ import annotations

from orchestrator.evaluation.judge import (
    DEFAULT_QC_CRITERIA,
    SCOPE_CRITERIA,
    Cassette,
    CassetteMiss,
    GatewayJudge,
    JudgeVerdict,
    dig,
    evaluate_judge,
    qc_correctness_evaluator,
    scope_adherence_evaluator,
)

__all__ = [
    "DEFAULT_QC_CRITERIA",
    "SCOPE_CRITERIA",
    "Cassette",
    "CassetteMiss",
    "GatewayJudge",
    "JudgeVerdict",
    "dig",
    "evaluate_judge",
    "qc_correctness_evaluator",
    "scope_adherence_evaluator",
]
