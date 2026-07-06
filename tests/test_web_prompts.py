"""Persistência de prompts do dashboard: store no servidor + contratos da UI.

Antes, os prompts viviam só no browser: templates em ``localStorage`` e o botão
"Salvar Prompts" apenas fechava o modal. Estes testes exigem um store JSON no
servidor (``.orchestrator/prompts.json``), endpoints ``/api/prompts`` e uma UI
que aplica templates via DOM (sem injeção de string em ``onclick``, que quebrava
com aspas duplas no prompt).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

import orchestrator.prompt_store as prompt_store
from orchestrator.config import default_prompt_store_path
from orchestrator.web import server as web_server


# ------------------------------------------------------------------ #
# prompt_store                                                       #
# ------------------------------------------------------------------ #

def test_default_prompt_store_path_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_PROMPTS", "/tmp/x/prompts.json")
    assert default_prompt_store_path() == Path("/tmp/x/prompts.json")


def test_default_prompt_store_path_default(monkeypatch) -> None:
    monkeypatch.delenv("ORCH_PROMPTS", raising=False)
    assert default_prompt_store_path() == Path(".orchestrator/prompts.json")


def test_save_template_returns_entry_and_persists(tmp_path) -> None:
    store = tmp_path / "prompts.json"
    saved = prompt_store.save_template(
        store, kind="creator", title="Meu Vlog", text='Prompt com "aspas"\ne linhas.',
        desc="desc",
    )
    assert saved["id"]
    assert saved["kind"] == "creator"
    assert saved["title"] == "Meu Vlog"
    assert saved["text"] == 'Prompt com "aspas"\ne linhas.'

    loaded = prompt_store.list_templates(store)
    assert loaded == [saved]


def test_list_templates_newest_first_and_filter_by_kind(tmp_path) -> None:
    store = tmp_path / "prompts.json"
    a = prompt_store.save_template(store, kind="creator", title="A", text="ta")
    b = prompt_store.save_template(store, kind="video", title="B", text="tb")
    c = prompt_store.save_template(store, kind="creator", title="C", text="tc")

    assert [t["id"] for t in prompt_store.list_templates(store)] == [c["id"], b["id"], a["id"]]
    assert [t["id"] for t in prompt_store.list_templates(store, kind="creator")] == [c["id"], a["id"]]
    assert [t["id"] for t in prompt_store.list_templates(store, kind="video")] == [b["id"]]


def test_save_template_rejects_bad_input(tmp_path) -> None:
    store = tmp_path / "prompts.json"
    with pytest.raises(ValueError):
        prompt_store.save_template(store, kind="banner", title="A", text="t")
    with pytest.raises(ValueError):
        prompt_store.save_template(store, kind="creator", title="", text="t")
    with pytest.raises(ValueError):
        prompt_store.save_template(store, kind="creator", title="A", text="")


def test_delete_template(tmp_path) -> None:
    store = tmp_path / "prompts.json"
    saved = prompt_store.save_template(store, kind="video", title="A", text="t")
    assert prompt_store.delete_template(store, saved["id"]) is True
    assert prompt_store.list_templates(store) == []
    assert prompt_store.delete_template(store, saved["id"]) is False


def test_record_and_get_last_used(tmp_path) -> None:
    store = tmp_path / "prompts.json"
    assert prompt_store.get_last_used(store) == {}

    prompt_store.record_last_used(store, creator_prompt="mulher 30 anos", video_prompt=None)
    assert prompt_store.get_last_used(store) == {"creator": "mulher 30 anos"}

    # Prompt vazio/None não apaga o último valor conhecido.
    prompt_store.record_last_used(store, creator_prompt=None, video_prompt="gancho: dor")
    assert prompt_store.get_last_used(store) == {
        "creator": "mulher 30 anos",
        "video": "gancho: dor",
    }


def test_last_used_does_not_leak_into_templates(tmp_path) -> None:
    store = tmp_path / "prompts.json"
    prompt_store.record_last_used(store, creator_prompt="x", video_prompt="y")
    prompt_store.save_template(store, kind="creator", title="A", text="t")
    assert [t["title"] for t in prompt_store.list_templates(store)] == ["A"]


# ------------------------------------------------------------------ #
# Endpoints /api/prompts                                             #
# ------------------------------------------------------------------ #

def test_prompts_endpoint_lists_templates_and_last_used(tmp_path, monkeypatch) -> None:
    store = tmp_path / "prompts.json"
    monkeypatch.setenv("ORCH_PROMPTS", str(store))
    prompt_store.save_template(store, kind="creator", title="A", text="ta")
    prompt_store.record_last_used(store, creator_prompt="ultimo", video_prompt=None)

    payload = asyncio.run(web_server.prompts_index())

    assert payload["store_path"] == str(store)
    assert payload["exists"] is True
    assert payload["last_used"] == {"creator": "ultimo"}
    assert [t["title"] for t in payload["templates"]] == ["A"]


def test_prompts_endpoint_saves_template(tmp_path, monkeypatch) -> None:
    store = tmp_path / "prompts.json"
    monkeypatch.setenv("ORCH_PROMPTS", str(store))

    req = web_server.PromptTemplateRequest(
        kind="video", title="Contrariano", text='Gancho: "verdade"', desc="d"
    )
    payload = asyncio.run(web_server.save_prompt_template(req))

    assert payload["ok"] is True
    assert payload["template"]["kind"] == "video"
    assert [t["title"] for t in prompt_store.list_templates(store)] == ["Contrariano"]


def test_prompts_endpoint_rejects_invalid_template(tmp_path, monkeypatch) -> None:
    store = tmp_path / "prompts.json"
    monkeypatch.setenv("ORCH_PROMPTS", str(store))

    req = web_server.PromptTemplateRequest(kind="banner", title="A", text="t")
    with pytest.raises(HTTPException) as err:
        asyncio.run(web_server.save_prompt_template(req))
    assert err.value.status_code == 422


def test_prompts_endpoint_deletes_template(tmp_path, monkeypatch) -> None:
    store = tmp_path / "prompts.json"
    monkeypatch.setenv("ORCH_PROMPTS", str(store))
    saved = prompt_store.save_template(store, kind="creator", title="A", text="t")

    payload = asyncio.run(web_server.delete_prompt_template(saved["id"]))
    assert payload["ok"] is True
    assert prompt_store.list_templates(store) == []

    with pytest.raises(HTTPException) as err:
        asyncio.run(web_server.delete_prompt_template(saved["id"]))
    assert err.value.status_code == 404


def test_start_run_records_last_used_prompts(tmp_path, monkeypatch) -> None:
    """Todo run com prompts registra o "último usado" — mesmo sem gate de aprovação."""
    store = tmp_path / "prompts.json"
    monkeypatch.setenv("ORCH_PROMPTS", str(store))

    req = web_server.RunRequest(
        offer="serum X",
        creator_prompt="mulher 30 anos",
        video_prompt="gancho: erro comum",
        approve_creators=False,
    )
    asyncio.run(web_server.start_run(req, BackgroundTasks()))

    assert prompt_store.get_last_used(store) == {
        "creator": "mulher 30 anos",
        "video": "gancho: erro comum",
    }


# ------------------------------------------------------------------ #
# Contratos estáticos da UI (index.html)                             #
# ------------------------------------------------------------------ #

def _html() -> str:
    return Path("src/orchestrator/web/static/index.html").read_text(encoding="utf-8")


def test_ui_templates_pane_is_dom_built_without_inline_injection() -> None:
    """Templates aplicam via listener DOM — injeção de prompt em onclick inline
    quebrava com aspas duplas (o clique silenciosamente não aplicava nada)."""
    html = _html()
    assert "function renderTemplatesPane" in html
    body = html.split("function renderTemplatesPane", 1)[1].split("\nasync function saveCustomTemplate", 1)[0]
    assert "addEventListener" in body
    assert "innerHTML" not in body
    # Nenhum card de template (builtin ou salvo) injeta prompt via onclick inline.
    assert "onclick=\"document.getElementById('modal-" not in html


def test_ui_templates_are_loaded_from_server_store() -> None:
    html = _html()
    assert 'fetch("/api/prompts"' in html
    assert "function migrateLocalTemplates" in html  # migra localStorage legado 1x


def test_ui_prompts_have_draft_persistence_and_apply() -> None:
    html = _html()
    assert "function saveDraftPrompts" in html
    assert "function restorePromptDrafts" in html
    assert "draft_creator_prompt" in html
    assert "draft_video_prompt" in html
    assert "function applyPrompts" in html
    assert 'onclick="applyPrompts()"' in html  # "Salvar Prompts" salva de verdade


def test_ui_main_form_shows_active_prompt_status() -> None:
    html = _html()
    assert 'id="prompt-status"' in html
    assert "function updatePromptStatus" in html


def test_ui_history_offers_prompt_reuse() -> None:
    html = _html()
    assert "function reusePromptsFromHistory" in html
    body = html.split("function renderHistory(creators", 1)[1].split("\n// Esc fecha", 1)[0]
    assert "reusePromptsFromHistory(" in body
