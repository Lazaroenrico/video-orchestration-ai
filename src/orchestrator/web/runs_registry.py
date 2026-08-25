"""Registro em memória dos runs locais servidos pelo dashboard.

Substitui o dict de módulo ``_runs`` do antigo ``web/server.py``. Semântica
preservada: puramente em memória (nada é persistido), chaveado por ``run_id``;
o estado inicial criado por :meth:`RunRegistry.create` continua sendo
``{"queues": [], "buffer": [], "done": False}``.

A instância canônica é o singleton ``REGISTRY``, consumida por executor e rotas
via acesso tardio (``runs_registry.REGISTRY``), o que permite substituição em
testes. O composition root a expõe em ``app.state.runs`` e mantém o apelido
``_runs`` por retrocompatibilidade com testes existentes.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

_MISSING = object()


class RunRegistry:
    """Mapa ``run_id -> estado runtime`` com o protocolo de dict usado hoje."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}

    def create(self, run_id: str) -> dict[str, Any]:
        """Cria o estado padrão de um run local e o retorna."""
        state: dict[str, Any] = {"queues": [], "buffer": [], "done": False}
        self._states[run_id] = state
        return state

    def get(
        self,
        run_id: str,
        default: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        return self._states.get(run_id, default)

    def __getitem__(self, run_id: str) -> dict[str, Any]:
        return self._states[run_id]

    def __setitem__(self, run_id: str, state: dict[str, Any]) -> None:
        self._states[run_id] = state

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._states

    def __len__(self) -> int:
        return len(self._states)

    def pop(self, run_id: str, default: Any = _MISSING) -> Any:
        if default is _MISSING:
            return self._states.pop(run_id)
        return self._states.pop(run_id, default)

    def clear(self) -> None:
        self._states.clear()

    def items(self):
        return self._states.items()


REGISTRY = RunRegistry()


def pending_creators_for(run_id: str) -> list[dict[str, Any]]:
    state = REGISTRY.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    creators = state.get("pending_creators")
    if not isinstance(creators, list) or not creators:
        raise HTTPException(status_code=409, detail="nenhum creator pendente para aprovação")
    return creators
