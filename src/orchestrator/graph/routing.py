"""Funções de roteamento usadas como conditional edges no subgrafo per-item.

- ``select_tier`` / ``route_after_script``: roteamento de tier (Step 4). Tentativas
  permanecem em LTX; ``attempts`` controla apenas o orçamento do loop de QC.
- ``route_after_qc``: o QC gate (Step 7). Aprovado -> montagem; reprovado dentro do
  orçamento -> regenera em LTX; esgotado -> descarta.
"""
from __future__ import annotations

from orchestrator.graph.state import Item


def select_tier(attempts: int, tier_names: list[str]) -> str:
    """Tier para a tentativa atual; retries continuam no primeiro tier (LTX)."""
    if not tier_names:
        raise ValueError("select_tier chamado sem tiers configurados")
    return tier_names[0]


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
