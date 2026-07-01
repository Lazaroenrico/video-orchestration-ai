"""Fixtures compartilhadas dos testes."""
import pytest

from orchestrator.adapters.mock import MockAdapter

# providers.yaml pode ter adapters reais (MVP). Garantir que todos os testes
# usem mock — testes que precisam de adapters reais optam via --live.
_MOCK_PROVIDERS = {
    "adapters": {r: "mock" for r in ("llm", "creator", "video", "qc", "assembly", "distribution")},
}


@pytest.fixture(autouse=True)
def _force_mock_providers(monkeypatch):
    # Patch no namespace do cli (importou load_providers por nome)
    monkeypatch.setattr("orchestrator.cli.load_providers", lambda *a, **kw: _MOCK_PROVIDERS)
    # Testes são offline por padrão; casos que exercitam tracing live optam
    # explicitamente via monkeypatch dentro do próprio teste.
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="LLM Judge: chama o gateway real (JUDGE_GATEWAY_URL) e regrava o cassette.",
    )


@pytest.fixture
def live(request) -> bool:
    return bool(request.config.getoption("--live"))

TIERS = [
    {
        "name": "ltx",
        "model": "lightricks/ltx-2.3-fast",
        "cost_per_second": 0.01,
        "max_concurrency": 16,
    },
    {"name": "kling", "model": "kling-3.0", "cost_per_second": 0.10, "max_concurrency": 6},
    {"name": "seedance", "model": "seedance-2.0", "cost_per_second": 0.168, "max_concurrency": 2},
]


@pytest.fixture
def pipeline_cfg():
    return {
        "batch": {"default_size": 6, "max_concurrency": 4},
        "qc": {"max_attempts": 3, "fail_rate": 0.34},
        "tiers": TIERS,
        "clip": {"duration_seconds": 8},
        "roster": {"creators": 2},
    }


@pytest.fixture
def adapter(pipeline_cfg):
    return MockAdapter(tiers=pipeline_cfg["tiers"])


@pytest.fixture
def run_config(adapter, pipeline_cfg):
    return {
        "configurable": {
            "adapter": adapter,
            "pipeline": pipeline_cfg,
            "run": {"platform": "tiktok"},
        },
        "max_concurrency": pipeline_cfg["batch"]["max_concurrency"],
        "recursion_limit": 50,
    }
