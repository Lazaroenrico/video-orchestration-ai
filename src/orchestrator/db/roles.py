"""Definição canônica de papéis RBAC e validação no domínio."""
from __future__ import annotations

MEMBERSHIP_ROLES: tuple[str, ...] = ("owner", "admin", "member", "viewer")
VALID_ROLES: frozenset[str] = frozenset(MEMBERSHIP_ROLES)


def validate_role(role: str) -> str:
    """Normaliza e valida se o papel fornecido pertence à matriz canônica de papéis."""
    if not isinstance(role, str):
        raise ValueError("papel inválido")
    normalized = role.strip().lower()
    if normalized not in VALID_ROLES:
        raise ValueError(f"papel de membership inválido: {role!r}")
    return normalized
