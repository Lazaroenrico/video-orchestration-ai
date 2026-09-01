"""Caracterização e validação da classificação de aliases e integrações legadas (Issue #8)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.adapters import base as adapters_base
from orchestrator.language_runtime import LanguageRuntime
from orchestrator.registry import _ADAPTERS, resolve_adapter

CLASSIFICATION_MAP = {
    # Supported
    "creator_vercel_elevenlabs_design": "supported",
    "integrity_qc": "supported",
    "replicate": "supported",
    "ffmpeg_assembly": "supported",
    "passthrough_upscale": "supported",
    "mock": "supported",
    "vercel_gateway_llm": "supported",
    # Compatibility
    "creator_real_vercel": "compatibility",
    "creator_real_replicate": "compatibility",
    "creator_vercel_replicate_voice": "compatibility",
    "creator_real": "compatibility",
    "ElevenLabsVoiceAdapter": "compatibility",
    "ReplicateVoiceAdapter": "compatibility",
    "anthropic_sdk_gateway": "compatibility",
    "anthropic": "compatibility",
    # Dead
    "TopazUpscaleAdapter": "dead",
    "ReplicateUpscaleAdapter": "dead",
    "vercel_gateway_video": "dead",
    "vercel_seedance_assembly": "dead",
    "scripts/vercel_generate_video.mjs": "dead",
    "root_package_json": "dead",
    "JudgePort": "dead",
}


def test_classification_map_has_all_categories() -> None:
    """Valida que todos os itens inventariados pertencem a uma das 3 categorias canônicas."""
    valid_categories = {"supported", "compatibility", "dead"}
    assert set(CLASSIFICATION_MAP.values()).issubset(valid_categories)
    assert len(CLASSIFICATION_MAP) >= 19


def test_versioned_configs_use_only_supported_adapters() -> None:
    """Garante que nenhum perfil versionado usa adapters classificados como dead ou compatibility."""
    root = Path(__file__).resolve().parents[1]
    config_dirs = [root / "config", root / "config-mock", root / "config-staging"]

    for config_dir in config_dirs:
        providers_file = config_dir / "providers.yaml"
        assert providers_file.exists(), f"missing {providers_file}"
        data = yaml.safe_load(providers_file.read_text(encoding="utf-8"))
        adapters = data.get("adapters", {})
        for role, adapter_name in adapters.items():
            assert adapter_name in CLASSIFICATION_MAP, (
                f"Adapter {adapter_name!r} em {config_dir.name} não está no inventário de classificação"
            )
            assert CLASSIFICATION_MAP[adapter_name] == "supported", (
                f"Perfil {config_dir.name} usa adapter {adapter_name!r} com classificação {CLASSIFICATION_MAP[adapter_name]!r} em vez de 'supported'"
            )


def test_dead_adapters_not_in_active_configs() -> None:
    """Garante que nenhum adapter dead é acionado em config/providers.yaml."""
    dead_items = {k for k, v in CLASSIFICATION_MAP.items() if v == "dead"}
    root = Path(__file__).resolve().parents[1]
    for config_name in ("config", "config-mock", "config-staging"):
        data = yaml.safe_load((root / config_name / "providers.yaml").read_text(encoding="utf-8"))
        for role, adapter_name in data.get("adapters", {}).items():
            assert adapter_name not in dead_items


def test_dead_files_and_bridges_are_purged() -> None:
    """Verifica que todos os arquivos classificados como dead foram fisicamente removidos."""
    root = Path(__file__).resolve().parents[1]
    dead_files = [
        root / "src" / "orchestrator" / "adapters" / "topaz_upscale.py",
        root / "src" / "orchestrator" / "adapters" / "replicate_upscale.py",
        root / "src" / "orchestrator" / "adapters" / "vercel_gateway_video.py",
        root / "src" / "orchestrator" / "adapters" / "vercel_seedance_assembly.py",
        root / "scripts" / "vercel_generate_video.mjs",
        root / "package.json",
        root / "package-lock.json",
        root / "tests" / "test_vercel_gateway_video.py",
        root / "tests" / "test_vercel_seedance_assembly.py",
        root / "tests" / "test_replicate_upscale.py",
    ]
    for dead_file in dead_files:
        assert not dead_file.exists(), f"Arquivo dead ainda existe: {dead_file}"


def test_dead_adapters_not_in_registry_map() -> None:
    """Garante que nenhum adapter dead consta no mapa de resoluções de _ADAPTERS."""
    dead_adapter_names = {
        "topaz_upscale",
        "replicate_upscale",
        "vercel_gateway_video",
        "vercel_seedance_assembly",
    }
    for name in dead_adapter_names:
        assert name not in _ADAPTERS, f"Adapter dead {name!r} ainda está registrado em _ADAPTERS"


def test_dockerfile_runtime_does_not_install_node() -> None:
    """Garante que o runtime Python no Dockerfile não instala nem copia Node/npm ou package.json raiz."""
    root = Path(__file__).resolve().parents[1]
    dockerfile_content = (root / "Dockerfile").read_text(encoding="utf-8")
    runtime_stage = dockerfile_content.split("FROM python:3.12-slim-bookworm AS runtime")[-1]
    assert "node" not in runtime_stage.lower() or "from=front-build /front/dist" in runtime_stage
    assert "COPY --from=front-build /usr/local/bin/node" not in runtime_stage
    assert "npm ci" not in runtime_stage


def test_image_upscalers_not_in_registry_adapters() -> None:
    """Upscalers de imagem (Topaz/Replicate) foram retirados do registry do Step 3."""
    assert "topaz_upscale" not in _ADAPTERS
    assert "replicate_upscale" not in _ADAPTERS


def test_judge_port_not_in_production_adapters_base() -> None:
    """JudgePort não deve residir nos protocolos de produção."""
    assert not hasattr(adapters_base, "JudgePort")


def test_compatibility_creator_aliases_resolvable_in_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aliases de compatibilidade de creator permanecem instanciáveis durante o prazo aprovado."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "test-token")
    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL", "mock/elevenlabs-voice")
    pipeline_cfg = {
        "tiers": [{"name": "fast", "cost_per_second": 0.05}],
        "voice": {"mode": "mock", "provider": "mock"},
    }
    for alias in ("creator_real_vercel", "creator_real_replicate", "creator_vercel_replicate_voice", "creator_real"):
        assert alias in _ADAPTERS
        adapter = resolve_adapter(alias, pipeline_cfg)
        assert adapter is not None


def test_language_runtime_providers_classification() -> None:
    """LanguageRuntime reconhece supported (mock, vercel_gateway_llm) e compatibility (anthropic, anthropic_sdk_gateway)."""
    # Supported
    mock_rt = LanguageRuntime.from_provider("mock", {})
    assert mock_rt.provider == "mock"
    vercel_rt = LanguageRuntime.from_provider("vercel_gateway_llm", {})
    assert vercel_rt.provider == "vercel_gateway_llm"

    # Compatibility
    anthropic_rt = LanguageRuntime.from_provider("anthropic", {})
    assert anthropic_rt.provider == "anthropic"
    anthropic_gw_rt = LanguageRuntime.from_provider("anthropic_sdk_gateway", {})
    assert anthropic_gw_rt.provider == "anthropic_sdk_gateway"

    # Unknown
    with pytest.raises(KeyError):
        LanguageRuntime.from_provider("unknown_provider", {})
