"""Unit tests for runtime contract persistence and validation."""
from __future__ import annotations

import copy
import json

import pytest

from orchestrator.agent_catalog import AgentCatalog, StageExecutionSpec, build_agent_catalog
from orchestrator.runtime_contract import (
    GRAPH_VERSION,
    SCHEMA_VERSION,
    LegacyPaidResumeBlockedError,
    RuntimeContract,
    RuntimeContractMismatchError,
    build_runtime_contract,
    validate_runtime_contract,
)


def _base_pipeline() -> dict:
    return {
        "batch": {"default_size": 2, "max_concurrency": 4},
        "tiers": [{"name": "ltx", "model": "lightricks/ltx-2.3-fast", "cost_per_second": 0.01}],
        "voice": {
            "mode": "designed",
            "provider": "elevenlabs",
            "design_model": "eleven_ttv_v3",
            "tts_model": "eleven_turbo_v2_5",
            "prompt_version": "voice-match-v1",
        },
        "latentsync": {"enabled": True, "model": "bytedance/latentsync"},
    }


def _base_providers() -> dict:
    return {
        "adapters": {
            "llm": "mock",
            "creator": "mock",
            "video": "mock",
            "qc": "mock",
            "assembly": "mock",
            "upscale": "mock",
        },
        "storage": {"backend": "local"},
    }


def test_runtime_contract_structure_and_constants():
    assert GRAPH_VERSION == "v2"
    assert SCHEMA_VERSION == "creative-v2"

    contract = build_runtime_contract(_base_pipeline(), _base_providers())
    assert isinstance(contract, RuntimeContract)
    assert contract.graph_version == GRAPH_VERSION
    assert contract.schema_version == SCHEMA_VERSION
    assert isinstance(contract.config_hash, str) and len(contract.config_hash) == 64
    assert isinstance(contract.fingerprint, str) and len(contract.fingerprint) == 64
    assert isinstance(contract.provider_aliases, dict)
    assert isinstance(contract.model_ids, dict)
    assert isinstance(contract.prompt_versions, dict)
    assert isinstance(contract.prompt_hashes, dict)

    data = contract.as_dict()
    assert data["graph_version"] == "v2"
    assert data["schema_version"] == "creative-v2"
    assert data["fingerprint"] == contract.fingerprint
    assert data["config_hash"] == contract.config_hash

    restored = RuntimeContract.from_dict(data)
    assert restored == contract
    assert restored.fingerprint == contract.fingerprint


def test_runtime_contract_strips_secrets_and_credentials():
    pipeline_with_secrets = _base_pipeline()
    pipeline_with_secrets["api_key"] = "super-secret-key-123"
    pipeline_with_secrets["nested"] = {
        "access_token": "token-xyz",
        "secret_token": "secret-xyz",
        "db_password": "mypassword",
        "private_key": "private-key-material",
    }
    providers_with_secrets = _base_providers()
    providers_with_secrets["r2_secret_access_key"] = "r2-secret-xyz"
    providers_with_secrets["auth_bearer"] = "bearer-token"

    contract = build_runtime_contract(pipeline_with_secrets, providers_with_secrets)
    serialized = json.dumps(contract.as_dict())

    assert "super-secret-key-123" not in serialized
    assert "token-xyz" not in serialized
    assert "secret-xyz" not in serialized
    assert "mypassword" not in serialized
    assert "private-key-material" not in serialized
    assert "r2-secret-xyz" not in serialized
    assert "bearer-token" not in serialized

    # Config hash remains the same regardless of secret values changing
    pipeline_different_secret = copy.deepcopy(pipeline_with_secrets)
    pipeline_different_secret["api_key"] = "different-secret-key-999"
    contract_diff_secret = build_runtime_contract(pipeline_different_secret, providers_with_secrets)
    assert contract_diff_secret.config_hash == contract.config_hash
    assert contract_diff_secret.fingerprint == contract.fingerprint


def test_runtime_contract_does_not_contain_prompt_bodies():
    catalog = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "agent",
                    "tools": ["generate_concepts"],
                    "agent_enabled": True,
                    "prompt_version": "concepts-v2",
                    "system_prompt_path": "prompts/agents/concepts.md",
                }
            }
        },
        base_dir="config-mock",
    )

    contract = build_runtime_contract(_base_pipeline(), _base_providers(), agent_catalog=catalog)
    as_dict = contract.as_dict()

    # Prompt version and prompt hash are present
    assert as_dict["prompt_versions"].get("concepts") == "concepts-v2"
    assert "concepts" in as_dict["prompt_hashes"]
    assert len(as_dict["prompt_hashes"]["concepts"]) == 64

    # No prompt body or system_prompt content is in the contract
    spec = catalog.stage("concepts")
    assert spec.system_prompt is not None and len(spec.system_prompt) > 20
    serialized = json.dumps(as_dict)
    assert spec.system_prompt not in serialized


def test_fingerprint_changes_on_config_or_model_or_prompt_change():
    base_pipe = _base_pipeline()
    base_prov = _base_providers()
    base_contract = build_runtime_contract(base_pipe, base_prov)

    # Change non-secret pipeline config (e.g. tier model)
    diff_tier_pipe = copy.deepcopy(base_pipe)
    diff_tier_pipe["tiers"][0]["model"] = "lightricks/ltx-2.3-hd"
    contract_diff_tier = build_runtime_contract(diff_tier_pipe, base_prov)
    assert contract_diff_tier.fingerprint != base_contract.fingerprint

    # Change provider adapter
    diff_prov = copy.deepcopy(base_prov)
    diff_prov["adapters"]["video"] = "replicate"
    contract_diff_prov = build_runtime_contract(base_pipe, diff_prov)
    assert contract_diff_prov.fingerprint != base_contract.fingerprint
    assert contract_diff_prov.provider_aliases["video"] == "replicate"

    # Change prompt version via catalog
    catalog_v1 = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "agent",
                    "tools": ["generate_concepts"],
                    "agent_enabled": True,
                    "prompt_version": "concepts-v1",
                    "system_prompt_path": "prompts/agents/concepts.md",
                }
            }
        },
        base_dir="config-mock",
    )
    catalog_v2 = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "agent",
                    "tools": ["generate_concepts"],
                    "agent_enabled": True,
                    "prompt_version": "concepts-v2",
                    "system_prompt_path": "prompts/agents/concepts.md",
                }
            }
        },
        base_dir="config-mock",
    )
    contract_v1 = build_runtime_contract(base_pipe, base_prov, agent_catalog=catalog_v1)
    contract_v2 = build_runtime_contract(base_pipe, base_prov, agent_catalog=catalog_v2)
    assert contract_v1.fingerprint != contract_v2.fingerprint


def test_validate_runtime_contract_matching_fingerprint():
    contract = build_runtime_contract(_base_pipeline(), _base_providers())
    # Should not raise
    validate_runtime_contract(contract, contract.as_dict(), is_paid=False)
    validate_runtime_contract(contract, contract.as_dict(), is_paid=True)
    validate_runtime_contract(contract.as_dict(), contract.as_dict(), is_paid=True)


def test_validate_runtime_contract_mismatch_raises():
    contract_a = build_runtime_contract(_base_pipeline(), _base_providers())
    diff_prov = copy.deepcopy(_base_providers())
    diff_prov["adapters"]["video"] = "replicate"
    contract_b = build_runtime_contract(_base_pipeline(), diff_prov)

    with pytest.raises(RuntimeContractMismatchError, match="fingerprint mismatch"):
        validate_runtime_contract(contract_a, contract_b.as_dict(), is_paid=False)

    with pytest.raises(RuntimeContractMismatchError, match="fingerprint mismatch"):
        validate_runtime_contract(contract_a, contract_b.as_dict(), is_paid=True)


def test_validate_runtime_contract_missing_persisted_contract():
    contract = build_runtime_contract(_base_pipeline(), _base_providers())

    # Mock / non-paid run allows missing contract (legacy compatibility)
    validate_runtime_contract(contract, None, is_paid=False)
    validate_runtime_contract(contract, {}, is_paid=False)

    # Paid run raises LegacyPaidResumeBlockedError
    with pytest.raises(LegacyPaidResumeBlockedError, match="Paid run without runtime contract"):
        validate_runtime_contract(contract, None, is_paid=True)

    with pytest.raises(LegacyPaidResumeBlockedError, match="Paid run without runtime contract"):
        validate_runtime_contract(contract, {}, is_paid=True)


def test_effective_llm_model_resolution_and_env_overrides(monkeypatch):
    mock_pipe = _base_pipeline()
    mock_prov = _base_providers()
    mock_contract = build_runtime_contract(mock_pipe, mock_prov)
    assert mock_contract.model_ids["llm"] == "mock"
    assert mock_contract.model_ids["concepts"] == "mock"
    assert mock_contract.model_ids["scripts"] == "mock"
    assert mock_contract.model_ids["creator_profiles"] == "mock"

    vercel_prov = copy.deepcopy(mock_prov)
    vercel_prov["adapters"]["llm"] = "vercel_gateway_llm"
    vercel_contract = build_runtime_contract(mock_pipe, vercel_prov)
    assert vercel_contract.model_ids["llm"] == "anthropic/claude-opus-4.8"
    assert vercel_contract.model_ids["concepts"] == "anthropic/claude-opus-4.8"
    assert vercel_contract.fingerprint != mock_contract.fingerprint

    # Overriding AI_GATEWAY_LLM_MODEL changes effective model and fingerprint
    monkeypatch.setenv("AI_GATEWAY_LLM_MODEL", "openai/gpt-4o")
    gateway_override_contract = build_runtime_contract(mock_pipe, vercel_prov)
    assert gateway_override_contract.model_ids["llm"] == "openai/gpt-4o"
    assert gateway_override_contract.model_ids["concepts"] == "openai/gpt-4o"
    assert gateway_override_contract.fingerprint != vercel_contract.fingerprint
    monkeypatch.delenv("AI_GATEWAY_LLM_MODEL", raising=False)

    # Overriding via pipeline config llm_model
    custom_pipe = copy.deepcopy(mock_pipe)
    custom_pipe["llm_model"] = "anthropic/claude-3-5-sonnet"
    custom_contract = build_runtime_contract(custom_pipe, vercel_prov)
    assert custom_contract.model_ids["llm"] == "anthropic/claude-3-5-sonnet"
    assert custom_contract.model_ids["concepts"] == "anthropic/claude-3-5-sonnet"
    assert custom_contract.fingerprint != vercel_contract.fingerprint

    # Stage target_model overrides stage-level model_id
    catalog_with_target = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "agent",
                    "tools": ["generate_concepts"],
                    "agent_enabled": True,
                    "target_model": "custom/stage-model",
                }
            }
        },
        base_dir="config-mock",
    )
    target_contract = build_runtime_contract(mock_pipe, vercel_prov, agent_catalog=catalog_with_target)
    assert target_contract.model_ids["concepts"] == "custom/stage-model"
    assert target_contract.model_ids["scripts"] == "anthropic/claude-opus-4.8"
    assert target_contract.fingerprint != vercel_contract.fingerprint


def test_creator_image_model_resolution_and_overrides(monkeypatch):
    mock_pipe = _base_pipeline()
    mock_prov = _base_providers()
    mock_contract = build_runtime_contract(mock_pipe, mock_prov)
    assert mock_contract.model_ids["creator_image"] == "mock"

    live_prov = copy.deepcopy(mock_prov)
    live_prov["adapters"]["creator"] = "creator_vercel_elevenlabs_design"
    live_contract = build_runtime_contract(mock_pipe, live_prov)
    assert live_contract.model_ids["creator_image"] == "openai/gpt-image-2"
    assert live_contract.fingerprint != mock_contract.fingerprint

    # Overriding AI_GATEWAY_OPENAI_MODEL
    monkeypatch.setenv("AI_GATEWAY_OPENAI_MODEL", "openai/dall-e-3")
    override_contract = build_runtime_contract(mock_pipe, live_prov)
    assert override_contract.model_ids["creator_image"] == "openai/dall-e-3"
    assert override_contract.fingerprint != live_contract.fingerprint
    monkeypatch.delenv("AI_GATEWAY_OPENAI_MODEL", raising=False)

    # Overriding via pipeline image.model
    custom_image_pipe = copy.deepcopy(mock_pipe)
    custom_image_pipe["image"] = {"model": "flux-pro"}
    custom_contract = build_runtime_contract(custom_image_pipe, live_prov)
    assert custom_contract.model_ids["creator_image"] == "flux-pro"
    assert custom_contract.fingerprint != live_contract.fingerprint


def test_agent_catalog_properties_in_contract_fingerprint():
    base_pipe = _base_pipeline()
    base_prov = _base_providers()

    catalog_base = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "agent",
                    "tools": ["generate_concepts"],
                    "agent_enabled": True,
                    "prompt_version": "concepts-v1",
                    "schema_version": "creative-v2",
                }
            }
        },
        base_dir="config-mock",
    )
    base_contract = build_runtime_contract(base_pipe, base_prov, agent_catalog=catalog_base)
    assert base_contract.stage_executors["concepts"] == "agent"
    assert base_contract.stage_tools["concepts"] == ["generate_concepts"]
    assert base_contract.stage_schema_versions["concepts"] == "creative-v2"
    assert base_contract.stage_agent_enabled["concepts"] is True

    # Serialization and deserialization roundtrip
    restored = RuntimeContract.from_dict(base_contract.as_dict())
    assert restored == base_contract

    # Change executor (agent -> tool)
    catalog_tool = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "tool",
                    "tools": ["generate_concepts"],
                    "agent_enabled": False,
                    "prompt_version": "concepts-v1",
                    "schema_version": "creative-v2",
                }
            }
        },
        base_dir="config-mock",
    )
    contract_tool = build_runtime_contract(base_pipe, base_prov, agent_catalog=catalog_tool)
    assert contract_tool.fingerprint != base_contract.fingerprint
    assert contract_tool.stage_executors["concepts"] == "tool"
    assert contract_tool.stage_agent_enabled["concepts"] is False

    # Change tools
    catalog_tools = AgentCatalog(
        stages=(
            StageExecutionSpec(
                stage="concepts",
                executor="agent",
                tools=("generate_concepts", "another_tool"),
                agent_enabled=True,
                schema_version="creative-v2",
            ),
        )
    )
    contract_tools = build_runtime_contract(base_pipe, base_prov, agent_catalog=catalog_tools)
    assert contract_tools.fingerprint != base_contract.fingerprint
    assert "another_tool" in contract_tools.stage_tools["concepts"]

    # Change schema_version
    catalog_schema = build_agent_catalog(
        {
            "stages": {
                "concepts": {
                    "executor": "agent",
                    "tools": ["generate_concepts"],
                    "agent_enabled": True,
                    "prompt_version": "concepts-v1",
                    "schema_version": "creative-v3",
                }
            }
        },
        base_dir="config-mock",
    )
    contract_schema = build_runtime_contract(base_pipe, base_prov, agent_catalog=catalog_schema)
    assert contract_schema.fingerprint != base_contract.fingerprint
    assert contract_schema.stage_schema_versions["concepts"] == "creative-v3"



