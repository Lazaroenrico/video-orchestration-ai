"""Contrato do RunRegistry (substituto in-memory do antigo dict global ``_runs``)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from orchestrator.web import server as web_server
from orchestrator.web.runs_registry import REGISTRY, RunRegistry, pending_creators_for


def test_create_uses_canonical_initial_state() -> None:
    registry = RunRegistry()
    state = registry.create("web-x")
    assert state == {"queues": [], "buffer": [], "done": False}
    assert registry["web-x"] is state


def test_dict_protocol_matches_previous_global_semantics() -> None:
    registry = RunRegistry()
    registry["r"] = {"queues": [], "buffer": [], "done": False}
    assert "r" in registry
    assert registry.get("nope") is None
    assert registry.get("nope", {}) == {}
    assert list(registry.items()) == [("r", registry["r"])]
    assert len(registry) == 1
    assert registry.pop("r")["done"] is False
    assert "r" not in registry
    assert registry.pop("r", None) is None
    with pytest.raises(KeyError):
        registry.pop("r")
    registry.clear()
    assert len(registry) == 0


def test_server_aliases_the_singleton_on_module_and_app_state() -> None:
    assert web_server._runs is REGISTRY
    assert web_server.app.state.runs is REGISTRY


def test_pending_creators_for_validates_run_and_payload() -> None:
    try:
        with pytest.raises(HTTPException) as not_found:
            pending_creators_for("nope")
        assert not_found.value.status_code == 404

        REGISTRY["empty"] = {}
        with pytest.raises(HTTPException) as conflict:
            pending_creators_for("empty")
        assert conflict.value.status_code == 409

        creators = [{"id": "c0"}]
        REGISTRY["ok"] = {"pending_creators": creators}
        assert pending_creators_for("ok") is creators
    finally:
        REGISTRY.clear()
