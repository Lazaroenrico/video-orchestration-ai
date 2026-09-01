"""Vocabulários canônicos de status de predictions de provedores."""

from __future__ import annotations

TERMINAL_PREDICTION_STATUSES = frozenset({"succeeded", "failed", "canceled"})
