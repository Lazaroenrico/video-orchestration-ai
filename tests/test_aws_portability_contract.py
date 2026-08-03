"""Contratos estáticos do exercício AWS da ADR-D36, Fase 6."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / "infra/aws-staging"


def test_aws_opentofu_uses_current_provider_and_zero_traffic_defaults():
    versions = (AWS / "versions.tf").read_text(encoding="utf-8")
    variables = (AWS / "variables.tf").read_text(encoding="utf-8")

    assert 'source  = "hashicorp/aws"' in versions
    assert 'version = "~> 6.56"' in versions
    assert 'default     = "sa-east-1"' in variables
    assert re.search(r'variable "api_desired_count".*?default\s*=\s*0', variables, re.S)
    assert re.search(
        r'variable "runner_desired_count".*?default\s*=\s*0',
        variables,
        re.S,
    )


def test_ecs_uses_one_immutable_image_for_api_and_runner():
    compute = (AWS / "compute.tf").read_text(encoding="utf-8")
    registry = (AWS / "registry.tf").read_text(encoding="utf-8")

    assert 'image_tag_mutability = "IMMUTABLE"' in registry
    assert "scan_on_push = true" in registry
    assert compute.count("local.immutable_image") >= 2
    assert '"orchestrator", "api"' in compute
    assert '"orchestrator", "sqs-runner"' in compute
    assert 'requires_compatibilities = ["FARGATE"]' in compute
    assert 'network_mode             = "awsvpc"' in compute
    assert 'cpu_architecture        = "X86_64"' in compute
    assert '"STORAGE_BACKEND", value = "dual"' in compute
    assert '"STORAGE_WRITE_BACKEND", value = "s3"' in compute
    assert '"ORCH_QUEUE_BACKEND", value = "sqs"' in compute


def test_aws_queue_storage_and_alerts_are_private_and_recoverable():
    data = (AWS / "data.tf").read_text(encoding="utf-8")
    monitoring = (AWS / "monitoring.tf").read_text(encoding="utf-8")

    assert 'resource "aws_sqs_queue" "wake"' in data
    assert 'resource "aws_sqs_queue" "wake_dlq"' in data
    assert re.search(r"maxReceiveCount\s*=\s*5", data)
    assert re.search(r"sqs_managed_sse_enabled\s*=\s*true", data)
    assert 'resource "aws_s3_bucket" "media"' in data
    assert 'resource "aws_s3_bucket_public_access_block" "media"' in data
    assert re.search(r"block_public_acls\s*=\s*true", data)
    assert 'status = "Enabled"' in data
    assert 'metric_name         = "ApproximateNumberOfMessagesVisible"' in monitoring
    assert 'metric_name         = "ApproximateAgeOfOldestMessage"' in monitoring


def test_cutover_runbook_preserves_preexisting_runs_and_uses_a_decision_gate():
    runbook = (ROOT / "docs/AWS-CUTOVER.md").read_text(encoding="utf-8")
    workflow = (
        ROOT / ".github/workflows/exercise-aws.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch" in workflow
    assert "tofu plan" in workflow
    assert "api_desired_count=0" in workflow
    assert "runner_desired_count=0" in workflow
    assert "pausar novos jobs" in runbook.lower()
    assert "drenar" in runbook.lower()
    assert "storage migrate-run" in runbook
    assert "STORAGE_BACKEND=dual" in runbook
    assert "STORAGE_WRITE_BACKEND=s3" in runbook
    assert "Last-Event-ID" in runbook
    assert "decisão Go/No-Go" in runbook


def test_aws_local_state_secrets_and_saved_plans_are_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    workflow = (
        ROOT / ".github/workflows/exercise-aws.yml"
    ).read_text(encoding="utf-8")

    for pattern in ("*.tfstate", "*.tfstate.*", "*.auto.tfvars", "*.tfplan"):
        assert pattern in gitignore
    assert "aws-no-traffic.tfplan" in workflow
