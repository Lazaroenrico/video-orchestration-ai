"""Testes de paridade para papéis canônicos entre Python e mapeamentos."""
from __future__ import annotations

from orchestrator.auth import ROLE_PERMISSIONS
from orchestrator.db.models import OrganizationInvitation, OrganizationMember
from orchestrator.db.roles import MEMBERSHIP_ROLES, VALID_ROLES, validate_role


def test_python_roles_parity_with_role_permissions():
    assert set(MEMBERSHIP_ROLES) == set(ROLE_PERMISSIONS.keys())
    assert VALID_ROLES == frozenset({"owner", "admin", "member", "viewer"})


def test_validate_role_normalizes_and_rejects_invalid():
    assert validate_role(" Owner ") == "owner"
    assert validate_role("ADMIN") == "admin"
    assert validate_role("member") == "member"
    assert validate_role("viewer") == "viewer"

    import pytest
    with pytest.raises(ValueError, match="papel de membership inválido"):
        validate_role("superuser")

    with pytest.raises(ValueError, match="papel inválido"):
        validate_role(123)  # type: ignore


def test_model_check_constraints_match_canonical_roles():
    # Verifica constraints declarativas nos modelos SQLAlchemy
    member_checks = [c.sqltext.text for c in OrganizationMember.__table__.constraints if hasattr(c, "sqltext")]
    assert any("role IN ('owner', 'admin', 'member', 'viewer')" in text for text in member_checks)

    invitation_checks = [c.sqltext.text for c in OrganizationInvitation.__table__.constraints if hasattr(c, "sqltext")]
    assert any("role IN ('owner', 'admin', 'member', 'viewer')" in text for text in invitation_checks)
