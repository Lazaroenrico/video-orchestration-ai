"""Runtime contract representation, canonical fingerprinting, and resume validation."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from orchestrator.agent_catalog import AgentCatalog, default_agent_catalog

GRAPH_VERSION = "v2"
SCHEMA_VERSION = "creative-v2"
DEFAULT_MODEL = "anthropic/claude-opus-4.8"

_SECRET_KEY_SUBSTRINGS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "auth",
    "credential",
    "private_key",
)


class RuntimeContractError(RuntimeError):
    """Base exception for runtime contract errors."""


class RuntimeContractMismatchError(RuntimeContractError):
    """Raised when the current runtime contract differs from the persisted one."""


class LegacyPaidResumeBlockedError(RuntimeContractError):
    """Raised when attempting to resume a paid run without a runtime contract fingerprint."""


def _resolve_effective_llm_model(
    provider: str,
    pipeline: dict[str, Any],
    target_model: str | None = None,
) -> str:
    if target_model:
        return str(target_model)
    if provider == "mock":
        default = "mock"
    elif provider == "anthropic":
        default = "claude-opus-4-8"
    else:
        default = DEFAULT_MODEL

    gateway_model = (
        os.environ.get("AI_GATEWAY_LLM_MODEL")
        if provider in {"vercel_gateway_llm", "anthropic_sdk_gateway", "default"}
        else None
    )
    return str(
        gateway_model
        or pipeline.get("llm_model")
        or pipeline.get("model")
        or default
    )


def _resolve_effective_creator_image_model(
    creator_adapter: str,
    pipeline: dict[str, Any],
) -> str:
    if creator_adapter == "mock":
        return "mock"
    image_config = pipeline.get("image")
    model_from_config = image_config.get("model") if isinstance(image_config, dict) else None
    default_model = "gpt-image-2" if creator_adapter == "creator_real" else "openai/gpt-image-2"
    return str(
        os.environ.get("AI_GATEWAY_OPENAI_MODEL")
        or model_from_config
        or default_model
    )


def _sanitize_config(obj: Any) -> Any:
    """Recursively strip sensitive keys and credentials from config dictionaries/lists."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in sorted(obj.items(), key=lambda item: str(item[0])):
            key_str = str(k).lower()
            if any(sub in key_str for sub in _SECRET_KEY_SUBSTRINGS):
                continue
            cleaned[str(k)] = _sanitize_config(v)
        return cleaned
    if isinstance(obj, (list, tuple)):
        return [_sanitize_config(item) for item in obj]
    if isinstance(obj, str):
        if "://" in obj and "@" in obj:
            return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", obj)
        return obj
    return obj


def _compute_config_hash(pipeline: dict[str, Any], providers: dict[str, Any]) -> str:
    sanitized = {
        "pipeline": _sanitize_config(pipeline or {}),
        "providers": _sanitize_config(providers or {}),
    }
    canonical_json = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _compute_fingerprint(data: dict[str, Any]) -> str:
    canonical = {
        "config_hash": data.get("config_hash", ""),
        "graph_version": data.get("graph_version", GRAPH_VERSION),
        "model_ids": dict(sorted((data.get("model_ids") or {}).items())),
        "prompt_hashes": dict(sorted((data.get("prompt_hashes") or {}).items())),
        "prompt_versions": dict(sorted((data.get("prompt_versions") or {}).items())),
        "provider_aliases": dict(sorted((data.get("provider_aliases") or {}).items())),
        "schema_version": data.get("schema_version", SCHEMA_VERSION),
        "stage_agent_enabled": dict(sorted((data.get("stage_agent_enabled") or {}).items())),
        "stage_executors": dict(sorted((data.get("stage_executors") or {}).items())),
        "stage_schema_versions": dict(sorted((data.get("stage_schema_versions") or {}).items())),
        "stage_tools": {
            k: list(v) for k, v in sorted((data.get("stage_tools") or {}).items())
        },
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RuntimeContract:
    config_hash: str
    fingerprint: str
    graph_version: str = GRAPH_VERSION
    schema_version: str = SCHEMA_VERSION
    provider_aliases: dict[str, str] = field(default_factory=dict)
    model_ids: dict[str, str] = field(default_factory=dict)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    prompt_hashes: dict[str, str] = field(default_factory=dict)
    stage_executors: dict[str, str] = field(default_factory=dict)
    stage_tools: dict[str, list[str]] = field(default_factory=dict)
    stage_schema_versions: dict[str, str] = field(default_factory=dict)
    stage_agent_enabled: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "graph_version": self.graph_version,
            "schema_version": self.schema_version,
            "config_hash": self.config_hash,
            "provider_aliases": dict(self.provider_aliases),
            "model_ids": dict(self.model_ids),
            "prompt_versions": dict(self.prompt_versions),
            "prompt_hashes": dict(self.prompt_hashes),
            "stage_executors": dict(self.stage_executors),
            "stage_tools": {k: list(v) for k, v in self.stage_tools.items()},
            "stage_schema_versions": dict(self.stage_schema_versions),
            "stage_agent_enabled": dict(self.stage_agent_enabled),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeContract:
        return cls(
            graph_version=str(data.get("graph_version", GRAPH_VERSION)),
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
            config_hash=str(data.get("config_hash", "")),
            provider_aliases=dict(data.get("provider_aliases") or {}),
            model_ids=dict(data.get("model_ids") or {}),
            prompt_versions=dict(data.get("prompt_versions") or {}),
            prompt_hashes=dict(data.get("prompt_hashes") or {}),
            stage_executors=dict(data.get("stage_executors") or {}),
            stage_tools={k: list(v) for k, v in (data.get("stage_tools") or {}).items()},
            stage_schema_versions=dict(data.get("stage_schema_versions") or {}),
            stage_agent_enabled=dict(data.get("stage_agent_enabled") or {}),
            fingerprint=str(data.get("fingerprint", "")),
        )


def build_runtime_contract(
    pipeline: dict[str, Any] | None = None,
    providers: dict[str, Any] | None = None,
    agent_catalog: AgentCatalog | None = None,
) -> RuntimeContract:
    pipe = pipeline or {}
    prov = providers or {}
    catalog = (
        agent_catalog
        if isinstance(agent_catalog, AgentCatalog)
        or (agent_catalog is not None and hasattr(agent_catalog, "stages"))
        else default_agent_catalog()
    )

    config_hash = _compute_config_hash(pipe, prov)

    raw_adapters = prov.get("adapters") if isinstance(prov, dict) and "adapters" in prov else prov
    provider_aliases: dict[str, str] = {}
    if isinstance(raw_adapters, dict):
        for k, v in raw_adapters.items():
            if v is not None:
                provider_aliases[str(k)] = str(v)

    llm_provider = str(provider_aliases.get("llm", "mock"))
    creator_adapter = str(provider_aliases.get("creator", "mock"))

    model_ids: dict[str, str] = {}
    model_ids["llm"] = _resolve_effective_llm_model(llm_provider, pipe)
    model_ids["creator_image"] = _resolve_effective_creator_image_model(creator_adapter, pipe)

    for spec in catalog.stages:
        model_ids[spec.stage] = _resolve_effective_llm_model(
            llm_provider, pipe, spec.target_model
        )

    if isinstance(pipe.get("voice"), dict):
        if pipe["voice"].get("design_model"):
            model_ids.setdefault("voice_design", str(pipe["voice"]["design_model"]))
        if pipe["voice"].get("tts_model"):
            model_ids.setdefault("voiceover", str(pipe["voice"]["tts_model"]))
    if isinstance(pipe.get("latentsync"), dict) and pipe["latentsync"].get("model"):
        model_ids.setdefault("latentsync", str(pipe["latentsync"]["model"]))
    for tier in pipe.get("tiers") or []:
        if isinstance(tier, dict) and tier.get("name") and tier.get("model"):
            model_ids.setdefault(f"video_{tier['name']}", str(tier["model"]))

    prompt_versions: dict[str, str] = {}
    prompt_hashes: dict[str, str] = {}
    for spec in catalog.stages:
        if spec.prompt_version:
            prompt_versions[spec.stage] = str(spec.prompt_version)
        if spec.prompt_hash:
            prompt_hashes[spec.stage] = str(spec.prompt_hash)
    if isinstance(pipe.get("voice"), dict) and pipe["voice"].get("prompt_version"):
        prompt_versions.setdefault("voice", str(pipe["voice"]["prompt_version"]))

    stage_executors = {spec.stage: spec.executor for spec in catalog.stages}
    stage_tools = {spec.stage: list(spec.tools) for spec in catalog.stages}
    stage_schema_versions = {
        spec.stage: spec.schema_version for spec in catalog.stages if spec.schema_version
    }
    stage_agent_enabled = {spec.stage: spec.agent_enabled for spec in catalog.stages}

    data = {
        "graph_version": GRAPH_VERSION,
        "schema_version": SCHEMA_VERSION,
        "config_hash": config_hash,
        "provider_aliases": provider_aliases,
        "model_ids": model_ids,
        "prompt_versions": prompt_versions,
        "prompt_hashes": prompt_hashes,
        "stage_executors": stage_executors,
        "stage_tools": stage_tools,
        "stage_schema_versions": stage_schema_versions,
        "stage_agent_enabled": stage_agent_enabled,
    }
    fingerprint = _compute_fingerprint(data)

    return RuntimeContract(
        graph_version=GRAPH_VERSION,
        schema_version=SCHEMA_VERSION,
        config_hash=config_hash,
        provider_aliases=provider_aliases,
        model_ids=model_ids,
        prompt_versions=prompt_versions,
        prompt_hashes=prompt_hashes,
        stage_executors=stage_executors,
        stage_tools=stage_tools,
        stage_schema_versions=stage_schema_versions,
        stage_agent_enabled=stage_agent_enabled,
        fingerprint=fingerprint,
    )


def validate_runtime_contract(
    current: RuntimeContract | dict[str, Any],
    persisted: dict[str, Any] | None,
    *,
    is_paid: bool = False,
) -> None:
    current_dict = current.as_dict() if isinstance(current, RuntimeContract) else current
    current_fingerprint = current_dict.get("fingerprint")

    if not persisted or not persisted.get("fingerprint"):
        if is_paid:
            raise LegacyPaidResumeBlockedError(
                "Paid run without runtime contract fingerprint cannot be resumed. Create a new run instead."
            )
        return

    persisted_fingerprint = persisted.get("fingerprint")
    if persisted_fingerprint != current_fingerprint:
        raise RuntimeContractMismatchError(
            f"Runtime contract fingerprint mismatch: persisted={persisted_fingerprint!r}, current={current_fingerprint!r}"
        )
