"""Overlay config-base/: deep-merge, expansão de env em todos os YAMLs e
resolução de prompts perfil→base. Rede de segurança contra drift silencioso."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from orchestrator.config import (
    load_agent_catalog,
    load_judge,
    load_pipeline,
    load_providers,
)

PROFILES = ("config", "config-mock", "config-staging")
SHARED_PROMPTS = ("_shared.md", "concepts.md", "scripts.md", "creators.md")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_tree(tmp_path: Path, *, with_base: bool) -> tuple[Path, Path]:
    """Cria <root>/profiles/<profile> e, opcionalmente, <root>/profiles/config-base."""
    root = tmp_path / "profiles"
    profile = root / "prof"
    profile.mkdir(parents=True)
    base = root / "config-base"
    if with_base:
        base.mkdir(parents=True)
    return profile, base


def test_deep_merge_overlay_wins_dicts_merge_and_lists_replaced(tmp_path):
    profile, base = _make_tree(tmp_path, with_base=True)
    _write(
        base / "pipeline.yaml",
        (
            "batch:\n"
            "  default_size: 12\n"
            "  max_concurrency: 8\n"
            "qc:\n"
            "  max_attempts: 3\n"
            "tiers:\n"
            "  - name: a\n"
            "    cost_per_second: 1.0\n"
            "base_only:\n"
            "  keep: true\n"
        ),
    )
    _write(
        profile / "pipeline.yaml",
        (
            "batch:\n"
            "  max_concurrency: 4\n"
            "qc:\n"
            "  fail_rate: 0.5\n"
            "tiers:\n"
            "  - name: b\n"
            "    cost_per_second: 2.0\n"
        ),
    )

    pipeline = load_pipeline(str(profile))

    # dict aninhado sofre merge profundo (overlay vence por chave)...
    assert pipeline["batch"] == {"default_size": 12, "max_concurrency": 4}
    assert pipeline["qc"] == {"max_attempts": 3, "fail_rate": 0.5}
    # ...lista é substituída wholesale pelo overlay...
    assert pipeline["tiers"] == [{"name": "b", "cost_per_second": 2.0}]
    # ...e chave exclusiva da base é preservada.
    assert pipeline["base_only"] == {"keep": True}


def test_env_expansion_applies_to_pipeline_providers_judge_and_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERLAY_TEST_URL", "https://example.invalid/hit")
    profile, _ = _make_tree(tmp_path, with_base=False)
    _write(profile / "pipeline.yaml", 'voice:\n  provider: "${OVERLAY_TEST_URL}"\n')
    _write(profile / "providers.yaml", 'storage:\n  backend: "${MISSING_VAR:-local}"\n')
    _write(
        profile / "judge.yaml",
        'gateway:\n  url: "${OVERLAY_TEST_URL}"\n  timeout_seconds: 30\n',
    )
    _write(
        profile / "agents.yaml",
        'stages:\n  concepts:\n    prompt_version: "${MISSING_PV:-concepts-v9}"\n',
    )

    assert load_pipeline(str(profile))["voice"]["provider"] == "https://example.invalid/hit"
    assert load_providers(str(profile))["storage"]["backend"] == "local"
    judge = load_judge(str(profile))
    assert judge["gateway"]["url"] == "https://example.invalid/hit"
    assert load_agent_catalog(str(profile)).stage("concepts").prompt_version == "concepts-v9"


def test_profile_without_sibling_base_loads_its_own_yaml_verbatim(tmp_path):
    profile, _ = _make_tree(tmp_path, with_base=False)
    _write(profile / "pipeline.yaml", "batch:\n  default_size: 3\n")

    assert load_pipeline(str(profile)) == {"batch": {"default_size": 3}}


def test_prompt_resolution_prefers_profile_then_falls_back_to_base(tmp_path):
    root = tmp_path / "profiles"
    base = root / "config-base"
    base.mkdir(parents=True)
    _write(base / "prompts" / "agents" / "_shared.md", "BASE-SHARED")
    _write(base / "prompts" / "agents" / "concepts.md", "BASE-CONCEPTS")

    overriding = root / "overriding"
    overriding.mkdir()
    _write(overriding / "prompts" / "agents" / "concepts.md", "PROFILE-CONCEPTS")
    _write(
        overriding / "agents.yaml",
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: agent\n"
            "    tools: [generate_concepts]\n"
            "    system_prompt_path: prompts/agents/concepts.md\n"
            "    agent_enabled: true\n"
        ),
    )

    inheriting = root / "inheriting"
    inheriting.mkdir()
    _write(
        inheriting / "agents.yaml",
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: agent\n"
            "    tools: [generate_concepts]\n"
            "    system_prompt_path: prompts/agents/concepts.md\n"
            "    agent_enabled: true\n"
        ),
    )

    over = load_agent_catalog(str(overriding)).stage("concepts").system_prompt or ""
    inh = load_agent_catalog(str(inheriting)).stage("concepts").system_prompt or ""

    assert "PROFILE-CONCEPTS" in over
    assert "BASE-SHARED" in over
    assert "PROFILE-CONCEPTS" not in inh
    assert "BASE-CONCEPTS" in inh
    assert "BASE-SHARED" in inh

    expected = hashlib.sha256("BASE-SHARED\n\nBASE-CONCEPTS".encode()).hexdigest()
    assert load_agent_catalog(str(inheriting)).stage("concepts").prompt_hash == expected


def test_missing_prompt_in_both_profile_and_base_fails_loud(tmp_path):
    root = tmp_path / "profiles"
    (root / "config-base").mkdir(parents=True)
    profile = root / "prof"
    profile.mkdir()
    _write(
        profile / "agents.yaml",
        (
            "stages:\n"
            "  concepts:\n"
            "    executor: agent\n"
            "    tools: [generate_concepts]\n"
            "    system_prompt_path: prompts/agents/concepts.md\n"
            "    agent_enabled: true\n"
        ),
    )

    with pytest.raises(ValueError, match="system_prompt_path not found"):
        load_agent_catalog(str(profile))


@pytest.mark.parametrize("profile", PROFILES)
def test_shared_prompts_resolve_from_base_without_silent_drift(profile: str):
    root = Path(__file__).resolve().parents[1]
    base_dir = root / "config-base" / "prompts" / "agents"
    profile_dir = root / profile / "prompts" / "agents"

    for name in SHARED_PROMPTS:
        expected = (base_dir / name).read_bytes()
        copy_in_profile = profile_dir / name
        # Presente no perfil apenas como override byte-idêntico; senão, ausente
        # (o loader resolve via config-base).
        if copy_in_profile.exists():
            assert copy_in_profile.read_bytes() == expected, (
                f"{profile}/prompts/agents/{name} divergiu de config-base; "
                "mova a divergência intencional para os perfis que precisam dela "
                "ou atualize a base explicitamente."
            )


def test_all_profiles_resolve_identical_creative_prompt_hashes():
    catalogs = {name: load_agent_catalog(name) for name in PROFILES}
    fingerprints = {
        name: tuple(
            catalog.stage(stage).prompt_hash
            for stage in ("concepts", "scripts", "creator_profiles")
        )
        for name, catalog in catalogs.items()
    }

    assert len(set(fingerprints.values())) == 1
    assert all(hash is not None for hash in fingerprints["config"])


def test_judge_yaml_lives_only_in_config_base():
    root = Path(__file__).resolve().parents[1]
    base_judge = root / "config-base" / "judge.yaml"

    assert base_judge.exists()
    merged = load_judge("config-mock")
    assert merged["gateway"]["method"] == "POST"
    assert merged["response"]["pass_threshold"] == 0.8
