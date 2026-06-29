"""Funções de roteamento usadas como conditional edges no subgrafo per-item.

- ``select_tier`` / ``route_after_script``: roteamento de tier (Step 4). Tentativas
  escalam o tier (LTX -> Kling -> Seedance), espelhando o Context.md (bulk barato,
  vencedores no premium).
- ``route_after_qc``: o QC gate (Step 7). Aprovado -> montagem; reprovado dentro do
  orçamento -> regenera no tier escalado; esgotado -> descarta.
"""
from __future__ import annotations

from orchestrator.graph.state import Item


def select_tier(attempts: int, tier_names: list[str]) -> str:
    """Tier para a tentativa atual; escala com ``attempts`` e satura no último."""
    idx = min(max(attempts, 0), len(tier_names) - 1)
    return tier_names[idx]


def route_after_script(item: Item, tier_names: list[str]) -> str:
    """Conditional edge após o script: escolhe o tier de geração."""
    return select_tier(item.attempts, tier_names)


def route_after_qc(item: Item, max_attempts: int, tier_names: list[str]) -> str:
    """QC gate: 'assembly' (aprovado), tier (regen) ou 'drop' (esgotado)."""
    if item.qc is None:
        raise ValueError("route_after_qc chamado sem QCResult no item")
    if item.qc.passed:
        return "assembly"
    if item.attempts < max_attempts:
        return select_tier(item.attempts, tier_names)
    return "drop"
