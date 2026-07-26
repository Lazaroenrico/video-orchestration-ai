"""Runner ECS: SQS acorda, PostgreSQL decide o trabalho."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from click.testing import CliRunner

from orchestrator.cli import cli
from orchestrator.sqs_runner import run_sqs_cycle, run_sqs_runner


async def test_sqs_cycle_publishes_outbox_processes_db_then_acks_signal():
    calls: list[str] = []

    class _Consumer:
        async def receive(self):
            calls.append("receive")
            return "receipt-1"

        async def ack(self, receipt):
            calls.append(f"ack:{receipt}")

    async def publish():
        calls.append("publish")
        return True

    async def process(*, worker_id):
        calls.append(f"process:{worker_id}")
        return True

    result = await run_sqs_cycle(
        _Consumer(),
        worker_id="ecs-1",
        publish_outbox=publish,
        process_job=process,
    )

    assert result == {
        "outbox_published": True,
        "signal_received": True,
        "job_processed": True,
    }
    assert calls == ["publish", "receive", "process:ecs-1", "ack:receipt-1"]


async def test_sqs_cycle_polls_database_without_signal_and_never_acks_failed_work():
    class _Consumer:
        def __init__(self, receipt):
            self.receipt = receipt
            self.acked = False

        async def receive(self):
            return self.receipt

        async def ack(self, _receipt):
            self.acked = True

    async def publish():
        return False

    async def idle(*, worker_id):
        assert worker_id == "ecs-2"
        return False

    idle_consumer = _Consumer(None)
    assert await run_sqs_cycle(
        idle_consumer,
        worker_id="ecs-2",
        publish_outbox=publish,
        process_job=idle,
    ) == {
        "outbox_published": False,
        "signal_received": False,
        "job_processed": False,
    }

    async def broken(*, worker_id):
        raise RuntimeError(worker_id)

    failed_consumer = _Consumer("receipt-failed")
    with pytest.raises(RuntimeError, match="ecs-3"):
        await run_sqs_cycle(
            failed_consumer,
            worker_id="ecs-3",
            publish_outbox=publish,
            process_job=broken,
        )
    assert failed_consumer.acked is False


async def test_sqs_runner_repeats_bounded_cycles_for_tests():
    class _Consumer:
        def __init__(self):
            self.receipts = iter(("r1", None))

        async def receive(self):
            return next(self.receipts)

        async def ack(self, _receipt):
            return None

    publish_calls = 0
    process_calls = 0

    async def publish():
        nonlocal publish_calls
        publish_calls += 1
        return False

    async def process(*, worker_id):
        nonlocal process_calls
        assert worker_id == "ecs-test"
        process_calls += 1
        return False

    result = await run_sqs_runner(
        worker_id="ecs-test",
        cycles=2,
        consumer=_Consumer(),
        publish_outbox=publish,
        process_job=process,
    )

    assert result["signal_received"] is False
    assert publish_calls == 2
    assert process_calls == 2


async def test_sqs_runner_builds_production_dependencies_and_requires_queue_env(
    monkeypatch,
):
    import orchestrator.sqs_runner as runner_module

    monkeypatch.delenv("SQS_QUEUE_URL", raising=False)
    with pytest.raises(ValueError, match="SQS_QUEUE_URL"):
        await run_sqs_runner(worker_id="ecs-prod", cycles=1)

    class _Consumer:
        async def receive(self):
            return None

        async def ack(self, _receipt):
            return None

    consumer = _Consumer()
    queue = object()
    repository_calls = 0

    @asynccontextmanager
    async def open_repository():
        nonlocal repository_calls
        repository_calls += 1
        yield object() if repository_calls == 1 else None

    async def publish(jobs, actual_queue, *, worker_id):
        assert jobs is not None
        assert actual_queue is queue
        assert worker_id == "ecs-prod-outbox"
        return True

    async def process(*, worker_id):
        assert worker_id == "ecs-prod"
        return False

    monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.test/wake")
    monkeypatch.setattr(runner_module, "SqsWakeConsumer", lambda _url: consumer)
    monkeypatch.setattr(runner_module, "SqsWakeQueue", lambda _url: queue)
    monkeypatch.setattr(runner_module.job_store, "open_repository", open_repository)
    monkeypatch.setattr(runner_module, "publish_outbox_once", publish)
    monkeypatch.setattr(runner_module, "run_worker_once", process)

    first = await run_sqs_runner(worker_id="ecs-prod", cycles=1)
    second = await run_sqs_runner(worker_id="ecs-prod", cycles=1)

    assert first["outbox_published"] is True
    assert second["outbox_published"] is False


def test_sqs_runner_cli_starts_the_ecs_daemon(monkeypatch):
    captured = {}

    async def fake_runner(**kwargs):
        captured.update(kwargs)
        return {
            "outbox_published": False,
            "signal_received": False,
            "job_processed": False,
        }

    monkeypatch.setattr("orchestrator.cli.run_sqs_runner", fake_runner)

    result = CliRunner().invoke(
        cli,
        ["sqs-runner", "--worker-id", "ecs-cli", "--cycles", "1"],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"worker_id": "ecs-cli", "cycles": 1}
