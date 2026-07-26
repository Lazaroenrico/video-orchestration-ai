"""Agent/model catalog configuration for D29 phase 3."""
from __future__ import annotations

import pytest


def test_missing_agents_yaml_loads_default_tool_catalog(tmp_path):
    from orchestrator.config import load_agent_catalog
    from orchestrator.tools.registry import TOOL_REGISTRY

    catalog = load_agent_catalog(str(tmp_path))

    assert {spec.stage for spec in catalog.stages} == {
        spec.stage for spec in TOOL_REGISTRY
    }
    assert all(spec.executor == "tool" for spec in catalog.stages)
    assert all(spec.agent_enabled is False for spec in catalog.stages)
    assert catalog.stage("concepts").tools == ("generate_concepts",)


def test_agents_yaml_overrides_declared_stage_and_keeps_other_defaults(tmp_path):
    from orchestrator.config import load_agent_catalog

    (tmp_path / "agents.yaml").write_text(
        "stages:\n"
        "  concepts:\n"
        "    executor: agent\n"
        "    tools: [generate_concepts]\n"
        "    target_model: claude-sonnet-4\n"
        "    target_agent: concept-agent\n"
        "    agent_enabled: true\n",
        encoding="utf-8",
    )

    catalog = load_agent_catalog(str(tmp_path))

    concepts = catalog.stage("concepts")
    scripts = catalog.stage("scripts")
    assert concepts.executor == "agent"
    assert concepts.tools == ("generate_concepts",)
    assert concepts.target_model == "claude-sonnet-4"
    assert concepts.target_agent == "concept-agent"
    assert concepts.agent_enabled is True
    assert scripts.executor == "tool"
    assert scripts.tools == ("write_script",)


def test_agents_yaml_null_stages_uses_default_catalog(tmp_path):
    from orchestrator.config import load_agent_catalog

    (tmp_path / "agents.yaml").write_text("stages: null\n", encoding="utf-8")

    catalog = load_agent_catalog(str(tmp_path))

    assert catalog.stage("concepts").executor == "tool"
    assert catalog.stage("concepts").tools == ("generate_concepts",)


def test_agent_catalog_serializes_to_stable_mapping(tmp_path):
    from orchestrator.config import load_agent_catalog

    prompt = tmp_path / "prompts" / "agents"
    prompt.mkdir(parents=True)
    (prompt / "_shared.md").write_text("Shared guardrails.", encoding="utf-8")
    (prompt / "concepts.md").write_text("Concept guardrails.", encoding="utf-8")
    (tmp_path / "agents.yaml").write_text(
        "stages:\n"
        "  concepts:\n"
        "    executor: agent\n"
        "    tools: [generate_concepts]\n"
        "    target_model: claude-sonnet-4\n"
        "    target_agent: concept-agent\n"
        "    system_prompt_path: prompts/agents/concepts.md\n"
        "    agent_enabled: true\n",
        encoding="utf-8",
    )

    data = load_agent_catalog(str(tmp_path)).as_dict()

    assert data["stages"]["concepts"] == {
        "executor": "agent",
        "tools": ["generate_concepts"],
        "target_model": "claude-sonnet-4",
        "target_agent": "concept-agent",
        "system_prompt_path": "prompts/agents/concepts.md",
        "has_system_prompt": True,
        "agent_enabled": True,
    }


def test_agents_yaml_resolves_stage_system_prompt_from_files(tmp_path):
    from orchestrator.config import load_agent_catalog

    prompt = tmp_path / "prompts" / "agents"
    prompt.mkdir(parents=True)
    (prompt / "_shared.md").write_text("Shared guardrails.", encoding="utf-8")
    (prompt / "concepts.md").write_text("Concept guardrails.", encoding="utf-8")
    (tmp_path / "agents.yaml").write_text(
        "stages:\n"
        "  concepts:\n"
        "    executor: agent\n"
        "    tools: [generate_concepts]\n"
        "    target_agent: concept-agent\n"
        "    system_prompt_path: prompts/agents/concepts.md\n"
        "    agent_enabled: true\n",
        encoding="utf-8",
    )

    spec = load_agent_catalog(str(tmp_path)).stage("concepts")

    assert spec.system_prompt_path == "prompts/agents/concepts.md"
    assert spec.system_prompt == "Shared guardrails.\n\nConcept guardrails."


def test_agents_yaml_resolves_stage_system_prompt_without_shared_file(tmp_path):
    from orchestrator.config import load_agent_catalog

    prompt = tmp_path / "prompts" / "agents"
    prompt.mkdir(parents=True)
    (prompt / "concepts.md").write_text("Concept guardrails.", encoding="utf-8")
    (tmp_path / "agents.yaml").write_text(
        "stages:\n"
        "  concepts:\n"
        "    executor: agent\n"
        "    tools: [generate_concepts]\n"
        "    system_prompt_path: prompts/agents/concepts.md\n"
        "    agent_enabled: true\n",
        encoding="utf-8",
    )

    spec = load_agent_catalog(str(tmp_path)).stage("concepts")

    assert spec.system_prompt == "Concept guardrails."


@pytest.mark.parametrize(
    ("filename", "body", "message"),
    [
        ("agents.yaml", "stages:\n  concepts:\n    executor: agent\n    tools: [generate_concepts]\n    system_prompt_path: prompts/agents/missing.md\n    agent_enabled: true\n", "system_prompt_path"),
        ("agents.yaml", "stages:\n  concepts:\n    executor: agent\n    tools: [generate_concepts]\n    system_prompt_path: ../outside.md\n    agent_enabled: true\n", "invalid system_prompt_path"),
        ("empty.md", "", "empty system prompt"),
    ],
)
def test_agents_yaml_rejects_invalid_system_prompt_files(tmp_path, filename, body, message):
    from orchestrator.config import load_agent_catalog

    prompt = tmp_path / "prompts" / "agents"
    prompt.mkdir(parents=True)
    (prompt / "_shared.md").write_text("Shared guardrails.", encoding="utf-8")
    if filename == "empty.md":
        (prompt / filename).write_text(body, encoding="utf-8")
        body = (
            "stages:\n"
            "  concepts:\n"
            "    executor: agent\n"
            "    tools: [generate_concepts]\n"
            f"    system_prompt_path: prompts/agents/{filename}\n"
            "    agent_enabled: true\n"
        )
    (tmp_path / "agents.yaml").write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_agent_catalog(str(tmp_path))


@pytest.mark.parametrize("config_dir", ["config", "config-mock"])
def test_project_config_dirs_ship_valid_agents_yaml(config_dir):
    from pathlib import Path

    from orchestrator.config import load_agent_catalog

    assert (Path(config_dir) / "agents.yaml").exists()
    catalog = load_agent_catalog(config_dir)

    # As tools por stage sao as mesmas nos dois perfis; o executor difere:
    # o perfil live (`config`) ativa o loop agentic nos stages LLM-only (Fase 0),
    # enquanto o perfil offline (`config-mock`) permanece em modo tool.
    assert catalog.stage("concepts").tools == ("generate_concepts",)
    assert catalog.stage("scripts").tools == ("write_script",)
    assert catalog.stage("persona").tools == ("write_persona",)
    assert catalog.stage("video").tools == ("generate_clip",)

    expected_executor = "agent" if config_dir == "config" else "tool"
    for stage in ("persona", "concepts", "scripts", "video"):
        spec = catalog.stage(stage)
        assert spec.system_prompt_path == f"prompts/agents/{stage}.md"
        assert spec.system_prompt
        assert spec.executor == expected_executor
        assert spec.agent_enabled is (expected_executor == "agent")


def test_runner_config_includes_agent_catalog(pipeline_cfg):
    from orchestrator.agent_catalog import default_agent_catalog
    from orchestrator.runner import _build_config

    catalog = default_agent_catalog()

    cfg = _build_config(
        pipeline_cfg,
        {"adapters": {"llm": "mock"}},
        run_id="run-catalog",
        platform="tiktok",
        agent_catalog=catalog,
    )

    assert cfg["configurable"]["agent_catalog"] is catalog


def test_cli_run_loads_and_passes_agent_catalog(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from orchestrator.agent_catalog import default_agent_catalog
    from orchestrator.cli import cli

    catalog = default_agent_catalog()
    observed = {}

    async def fake_run_pipeline(*args, **kwargs):
        observed["agent_catalog"] = kwargs["agent_catalog"]
        return "cli-catalog", {"results": []}

    monkeypatch.setattr("orchestrator.cli.load_pipeline", lambda config_dir=None: {})
    monkeypatch.setattr("orchestrator.cli.load_providers", lambda config_dir=None: {})
    monkeypatch.setattr("orchestrator.cli.load_agent_catalog", lambda config_dir=None: catalog)
    monkeypatch.setattr("orchestrator.cli.runner.run_pipeline", fake_run_pipeline)

    result = CliRunner().invoke(
        cli,
        ["run", "--run-id", "cli-catalog", "--db", str(tmp_path / "runs.sqlite")],
        env={"LANGSMITH_TRACING": "false"},
    )

    assert result.exit_code == 0, result.output
    assert observed["agent_catalog"] is catalog


async def test_web_execute_run_injects_agent_catalog(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from orchestrator.agent_catalog import default_agent_catalog
    from orchestrator.web import server as web_server

    catalog = default_agent_catalog()
    observed = {}

    class _Checkpoint:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Graph:
        async def astream_events(self, resume_input, cfg, version):
            observed["agent_catalog"] = cfg["configurable"]["agent_catalog"]
            yield {
                "event": "on_chain_end",
                "name": "LangGraph",
                "metadata": {},
                "data": {"output": {"results": []}},
            }

        async def aget_state(self, cfg):
            return SimpleNamespace(tasks=[], next=(), values={"results": []})

    monkeypatch.setattr(web_server, "load_pipeline", lambda config_dir=None: {})
    monkeypatch.setattr(web_server, "load_providers", lambda config_dir=None: {})
    monkeypatch.setattr(web_server, "load_agent_catalog", lambda config_dir=None: catalog)
    monkeypatch.setattr(web_server, "build_adapter_from_providers", lambda providers, pipeline: object())
    monkeypatch.setattr(web_server, "open_checkpointer", lambda db_path: _Checkpoint())
    monkeypatch.setattr(web_server, "build_graph", lambda pipeline, checkpointer=None: _Graph())

    web_server._runs["web-catalog"] = {"queues": [], "buffer": [], "done": False}

    await web_server._execute_run(
        "web-catalog",
        offer="serum X",
        batch=1,
        platform="tiktok",
        config_dir="config",
        db_path=str(tmp_path / "runs.sqlite"),
        approve_creators=False,
        edit_concepts=False,
    )

    assert observed["agent_catalog"] is catalog


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "stages:\n"
            "  nope:\n"
            "    executor: tool\n",
            "unknown stage 'nope'",
        ),
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: worker\n"
            "    tools: [generate_concepts]\n",
            "invalid executor 'worker'",
        ),
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: agent\n"
            "    tools: [generate_concepts]\n"
            "    agent_enabled: false\n",
            "requires agent_enabled: true",
        ),
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: tool\n"
            "    tools: [generate_concepts]\n"
            "    agent_enabled: true\n",
            "requires executor: agent",
        ),
        (
            # roster segue fora do gate de agent (video entrou no D33).
            "stages:\n"
            "  roster:\n"
            "    executor: agent\n"
            "    tools: [build_creator]\n"
            "    agent_enabled: true\n",
            "only supported for stages",
        ),
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: tool\n"
            "    tools: [missing_tool]\n",
            "unknown tool 'missing_tool'",
        ),
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: tool\n"
            "    tools: [write_script]\n",
            "belongs to stage 'scripts'",
        ),
        (
            "stages: []\n",
            "stages must be a mapping",
        ),
        (
            "stages:\n"
            "  concepts: []\n",
            "stage 'concepts' must be a mapping",
        ),
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: tool\n"
            "    tools: []\n",
            "tools must be a non-empty list",
        ),
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: tool\n"
            "    tools: generate_concepts\n",
            "tools must be a non-empty list",
        ),
    ],
)
def test_agents_yaml_validation_errors_are_actionable(tmp_path, body, message):
    from orchestrator.config import load_agent_catalog

    (tmp_path / "agents.yaml").write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_agent_catalog(str(tmp_path))


def test_agent_catalog_stage_lookup_rejects_unknown_stage():
    from orchestrator.agent_catalog import default_agent_catalog

    with pytest.raises(KeyError, match="unknown"):
        default_agent_catalog().stage("unknown")


def test_video_is_an_allowed_agent_stage():
    """D33: video entra no gate de agent execution; a demais mídia segue fora."""
    from orchestrator.agent_catalog import (
        agent_stage_not_allowed_message,
        is_agent_stage_allowed,
    )

    assert is_agent_stage_allowed("video") is True
    for stage in ("roster", "assembly", "upscale", "qc"):
        assert is_agent_stage_allowed(stage) is False
    assert "video" in agent_stage_not_allowed_message()
