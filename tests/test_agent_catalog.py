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
        "    materializer: generate_concepts\n"
        "    target_model: claude-sonnet-4\n"
        "    agent_enabled: true\n",
        encoding="utf-8",
    )

    catalog = load_agent_catalog(str(tmp_path))

    concepts = catalog.stage("concepts")
    scripts = catalog.stage("scripts")
    assert concepts.executor == "agent"
    assert concepts.materializer == "generate_concepts"
    assert concepts.tools == ("generate_concepts",)
    assert concepts.target_model == "claude-sonnet-4"
    assert concepts.agent_enabled is True
    assert scripts.executor == "tool"
    assert scripts.materializer == "write_script"
    assert scripts.tools == ("write_script",)


def test_agents_yaml_normalizes_legacy_tools_list_to_materializer(tmp_path):
    from orchestrator.config import load_agent_catalog

    (tmp_path / "agents.yaml").write_text(
        "stages:\n"
        "  concepts:\n"
        "    executor: agent\n"
        "    tools: [generate_concepts]\n"
        "    target_agent: concept-agent\n"
        "    agent_enabled: true\n",
        encoding="utf-8",
    )

    catalog = load_agent_catalog(str(tmp_path))
    concepts = catalog.stage("concepts")
    assert concepts.executor == "agent"
    assert concepts.materializer == "generate_concepts"
    assert concepts.tools == ("generate_concepts",)


def test_agents_yaml_null_stages_uses_default_catalog(tmp_path):
    from orchestrator.config import load_agent_catalog

    (tmp_path / "agents.yaml").write_text("stages: null\n", encoding="utf-8")

    catalog = load_agent_catalog(str(tmp_path))

    assert catalog.stage("concepts").executor == "tool"
    assert catalog.stage("concepts").materializer == "generate_concepts"
    assert catalog.stage("concepts").tools == ("generate_concepts",)


def test_agent_catalog_serializes_to_stable_mapping(tmp_path):
    import hashlib

    from orchestrator.config import load_agent_catalog

    prompt = tmp_path / "prompts" / "agents"
    prompt.mkdir(parents=True)
    (prompt / "_shared.md").write_text("Shared guardrails.", encoding="utf-8")
    (prompt / "concepts.md").write_text("Concept guardrails.", encoding="utf-8")
    (tmp_path / "agents.yaml").write_text(
        "stages:\n"
        "  concepts:\n"
        "    executor: agent\n"
        "    materializer: generate_concepts\n"
        "    target_model: claude-sonnet-4\n"
        "    system_prompt_path: prompts/agents/concepts.md\n"
        "    agent_enabled: true\n",
        encoding="utf-8",
    )

    data = load_agent_catalog(str(tmp_path)).as_dict()

    stage = data["stages"]["concepts"]
    assert stage == {
        "executor": "agent",
        "materializer": "generate_concepts",
        "target_model": "claude-sonnet-4",
        "has_system_prompt": True,
        "prompt_version": None,
        "prompt_hash": hashlib.sha256(
            b"Shared guardrails.\n\nConcept guardrails."
        ).hexdigest(),
        "schema_version": None,
        "agent_enabled": True,
    }
    assert "system_prompt" not in stage
    assert "system_prompt_path" not in stage
    assert "target_agent" not in stage
    assert "tools" not in stage


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

    # Only the three bounded creative stages receive hidden phase prompts.
    assert catalog.stage("concepts").materializer == "generate_concepts"
    assert catalog.stage("scripts").materializer == "write_script"
    assert catalog.stage("creator_profiles").materializer == "design_creator_roster"
    assert set(catalog.as_dict()["stages"].keys()) == {"concepts", "scripts", "creator_profiles"}

    expected_executor = "agent" if config_dir == "config" else "tool"
    prompt_files = {
        "concepts": "concepts.md",
        "scripts": "scripts.md",
        "creator_profiles": "creators.md",
    }
    for stage, filename in prompt_files.items():
        spec = catalog.stage(stage)
        assert spec.system_prompt_path == f"prompts/agents/{filename}"
        assert spec.system_prompt
        assert spec.executor == expected_executor
        assert spec.agent_enabled is (expected_executor == "agent")
        assert spec.prompt_version
        assert spec.prompt_hash
        assert spec.schema_version == "creative-v2"


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


async def test_web_execute_run_injects_agent_catalog(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from orchestrator.agent_catalog import default_agent_catalog
    from orchestrator.web import server as web_server

    catalog = default_agent_catalog()
    observed = {}

    class FakeDependencies:
        adapter = object()

        def configurable(self, *, run_id, platform, run_options=None):
            observed["catalog"] = catalog
            return {
                "thread_id": run_id,
                "platform": platform,
                "agent_catalog": catalog,
            }

    class Checkpoint:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    class Repository:
        async def save(self, *_args, **_kwargs):
            return None

    class Graph:
        async def astream_events(self, _input, config, version):
            observed["agent_catalog"] = config["configurable"]["agent_catalog"]
            yield {"event": "on_chain_end", "name": "LangGraph", "metadata": {}, "data": {"output": {"results": []}}}

        async def aget_state(self, _config):
            return SimpleNamespace(tasks=[], next=(), values={"results": []})

    monkeypatch.setattr(web_server, "load_pipeline", lambda _path: {})
    monkeypatch.setattr(web_server, "load_providers", lambda _path: {})
    monkeypatch.setattr(web_server, "load_agent_catalog", lambda _path: catalog)
    monkeypatch.setattr(web_server.RunDependencies, "build", lambda *_a, **_k: FakeDependencies())
    monkeypatch.setattr(web_server, "open_checkpointer", lambda _path: Checkpoint())
    monkeypatch.setattr(web_server, "build_graph", lambda *_a, **_k: Graph())
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
        campaign_payload={
            "offer": "serum X", "audience": "adults", "platform": "tiktok", "batch_size": 1
        },
        _run_repository=Repository(),
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


def test_only_bounded_creative_stages_are_allowed_agents():
    from orchestrator.agent_catalog import (
        agent_stage_not_allowed_message,
        is_agent_stage_allowed,
    )

    for stage in ("concepts", "scripts", "creator_profiles"):
        assert is_agent_stage_allowed(stage) is True
    for stage in ("persona", "video", "roster", "assembly", "upscale", "qc"):
        assert is_agent_stage_allowed(stage) is False
    message = agent_stage_not_allowed_message()
    assert "concepts" in message
    assert "creator_profiles" in message
    assert "scripts" in message
