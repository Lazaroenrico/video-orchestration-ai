"""Testes unitários para normalização de e-mail e contratos de convites."""
from __future__ import annotations

import pytest

from orchestrator.db.invitations import (
    normalize_email,
)


def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  User@Example.COM  ") == "user@example.com"
    assert normalize_email("ALICE.smith+tag@DOMAIN.org ") == "alice.smith+tag@domain.org"


def test_normalize_email_rejects_empty_or_invalid():
    with pytest.raises(ValueError, match="e-mail inválido"):
        normalize_email("")
    with pytest.raises(ValueError, match="e-mail inválido"):
        normalize_email("   ")
    with pytest.raises(ValueError, match="e-mail inválido"):
        normalize_email("invalid-email-without-at")
    with pytest.raises(ValueError, match="e-mail inválido"):
        normalize_email("@no-local-part.com")
    with pytest.raises(ValueError, match="e-mail inválido"):
        normalize_email("no-domain@")
