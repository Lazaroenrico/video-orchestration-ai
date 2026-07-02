"""TDD para orchestrator.logging_config — setup de logging do entrypoint.

Offline: só configura o root logger; não faz rede. O objetivo é que os
``_log.error(...)`` dos caminhos best-effort (upscale/voz/roster) apareçam com
timestamp, nível e contexto, em vez de sumirem no 'last resort' do Python.
"""
from __future__ import annotations

import logging


def test_configure_logging_sets_level_from_env(monkeypatch):
    from orchestrator.logging_config import configure_logging

    monkeypatch.setenv("ORCHESTRATOR_LOG_LEVEL", "DEBUG")
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_defaults_to_info(monkeypatch):
    from orchestrator.logging_config import configure_logging

    monkeypatch.delenv("ORCHESTRATOR_LOG_LEVEL", raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_is_idempotent(monkeypatch):
    from orchestrator.logging_config import configure_logging

    monkeypatch.delenv("ORCHESTRATOR_LOG_LEVEL", raising=False)
    configure_logging()
    configure_logging()
    root = logging.getLogger()
    owned = [h for h in root.handlers if getattr(h, "_orchestrator_handler", False)]
    assert len(owned) == 1


def test_configure_logging_invalid_level_falls_back_to_info(monkeypatch):
    from orchestrator.logging_config import configure_logging

    monkeypatch.setenv("ORCHESTRATOR_LOG_LEVEL", "NOPE")
    configure_logging()
    assert logging.getLogger().level == logging.INFO
