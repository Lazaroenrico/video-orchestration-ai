"""Contratos estáticos do staging Cloudflare/Neon; não exigem credenciais."""
from __future__ import annotations

import json
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _jsonc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_staging_keeps_all_generation_adapters_mock_and_moves_bytes_to_r2():
    providers = yaml.safe_load(
        (ROOT / "config-staging/providers.yaml").read_text(encoding="utf-8")
    )

    assert set(providers["adapters"].values()) <= {"mock", "gateway"}
    assert all(
        providers["adapters"][role] == "mock"
        for role in ("llm", "creator", "video", "qc", "assembly", "upscale")
    )
    assert providers["storage"] == {"backend": "r2"}


def test_wrangler_routes_spa_api_sse_and_uses_distinct_container_roles():
    config = _jsonc(ROOT / "deploy/cloudflare/wrangler.jsonc")
    containers = {item["class_name"]: item for item in config["containers"]}

    assert config["assets"]["directory"] == "../../front/dist"
    assert config["assets"]["not_found_handling"] == "single-page-application"
    assert config["assets"]["run_worker_first"] == ["/api/*"]
    assert containers["ApiContainer"]["instance_type"] == "basic"
    assert containers["ApiContainer"]["max_instances"] == 2
    assert containers["RunnerContainer"]["instance_type"] == "standard-2"
    assert containers["RunnerContainer"]["max_instances"] == 1
    assert containers["ApiContainer"]["image"] == containers["RunnerContainer"]["image"]
    assert config["triggers"]["crons"] == ["*/1 * * * *"]
    assert config["queues"]["producers"][0]["binding"] == "WAKE_QUEUE"
    assert config["queues"]["consumers"][0]["dead_letter_queue"] == "ugc-wake-dlq-staging"


def test_worker_forwards_access_jwt_injects_tenant_and_only_wakes_runner():
    worker = (ROOT / "deploy/cloudflare/src/index.ts").read_text(encoding="utf-8")

    assert 'headers.set("X-Orch-Organization-Slug"' in worker
    assert 'headers.set("X-Orch-Organization-Name"' in worker
    assert "Cf-Access-Jwt-Assertion" in worker
    assert 'url.pathname.startsWith("/api/")' in worker
    assert "batch.messages" in worker
    assert "/internal/runner/once" in worker
    assert "message.ack()" in worker
    assert "message.retry()" in worker
    assert "run_worker_once" not in worker


def test_opentofu_pins_current_providers_and_declares_security_resources():
    versions = (ROOT / "infra/staging/versions.tf").read_text(encoding="utf-8")
    cloudflare = (ROOT / "infra/staging/cloudflare.tf").read_text(encoding="utf-8")
    neon = (ROOT / "infra/staging/neon.tf").read_text(encoding="utf-8")

    assert 'source  = "cloudflare/cloudflare"' in versions
    assert 'version = "~> 5.22"' in versions
    assert 'source  = "terraform-community-providers/neon"' in versions
    assert 'version = "~> 0.1.15"' in versions
    assert 'resource "cloudflare_r2_bucket"' in cloudflare
    assert 'resource "cloudflare_queue"' in cloudflare
    assert 'resource "cloudflare_zero_trust_access_application"' in cloudflare
    assert 'resource "cloudflare_ruleset"' in cloudflare
    assert re.search(r'phase\s*=\s*"http_ratelimit"', cloudflare)
    assert re.search(r'region_id\s*=\s*"aws-sa-east-1"', neon)
    assert re.search(r"pg_version\s*=\s*16", neon)
    assert re.search(r"history_retention\s*=\s*604800", neon)
    assert re.search(r"suspend_timeout\s*=\s*300", neon)


def test_staging_docs_forbid_pooler_for_migrations_and_checkpoints():
    runbook = (ROOT / "docs/STAGING.md").read_text(encoding="utf-8")

    assert "MIGRATION_DATABASE_URL" in runbook
    assert "DATABASE_URL" in runbook
    assert "pooled" in runbook.lower()
    assert "não use" in runbook.lower()
    assert "aws-sa-east-1" in runbook
    assert "membership-grant" in runbook


def test_deploy_workflow_builds_one_digest_migrates_then_rolls_out():
    workflow = (
        ROOT / ".github/workflows/deploy-staging.yml"
    ).read_text(encoding="utf-8")

    assert "docker/build-push-action" in workflow
    assert "linux/amd64" in workflow
    assert "github.sha" in workflow
    assert '"$IMAGE" migrate' in workflow
    assert "needs: [build, migrate]" in workflow
    assert "wrangler deploy" in workflow
    assert "containers-rollout" in workflow
    assert ":latest" not in workflow
