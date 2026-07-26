"""Backends de wake-up: PostgreSQL continua sendo a fila canônica."""
from __future__ import annotations

import json

import httpx
import pytest

from orchestrator import wake_queue as wake_queue_module
from orchestrator.wake_queue import (
    CloudflareWakeQueue,
    DatabaseWakeQueue,
    SqsWakeConsumer,
    SqsWakeQueue,
    build_wake_queue,
)


async def test_database_wake_queue_is_a_deliberate_noop():
    queue = DatabaseWakeQueue()

    await queue.publish(
        topic="run.queued",
        message_key="job-1",
        payload={"job_id": "job-1"},
    )


async def test_cloudflare_wake_queue_pushes_json_with_bearer_auth():
    observed = {}

    async def handler(request):
        observed["authorization"] = request.headers["authorization"]
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    queue = CloudflareWakeQueue(
        "https://api.cloudflare.test/messages",
        "queue-token",
        client=client,
    )

    await queue.publish(
        topic="run.queued",
        message_key="job-1",
        payload={"job_id": "job-1"},
    )
    await client.aclose()

    assert observed == {
        "authorization": "Bearer queue-token",
        "body": {
            "body": {
                "topic": "run.queued",
                "message_key": "job-1",
                "payload": {"job_id": "job-1"},
            },
            "content_type": "json",
        },
    }


async def test_sqs_wake_queue_sends_deterministic_message_body():
    class FakeSqs:
        def __init__(self):
            self.calls = []

        def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return {"MessageId": "message-1"}

    client = FakeSqs()

    async def inline(function, **kwargs):
        return function(**kwargs)

    queue = SqsWakeQueue(
        "https://sqs.test/queue",
        client=client,
        run_sync=inline,
    )

    await queue.publish(
        topic="run.resume",
        message_key="job-2",
        payload={"job_id": "job-2"},
    )

    assert client.calls == [
        {
            "QueueUrl": "https://sqs.test/queue",
            "MessageBody": (
                '{"message_key":"job-2","payload":{"job_id":"job-2"},'
                '"topic":"run.resume"}'
            ),
        }
    ]


def test_wake_queue_factory_defaults_to_database_and_fails_fast(monkeypatch):
    monkeypatch.delenv("ORCH_QUEUE_BACKEND", raising=False)
    assert isinstance(build_wake_queue(), DatabaseWakeQueue)

    monkeypatch.setenv("ORCH_QUEUE_BACKEND", "cloudflare")
    monkeypatch.delenv("CF_QUEUE_PUSH_URL", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="CF_QUEUE_PUSH_URL"):
        build_wake_queue()

    monkeypatch.setenv("ORCH_QUEUE_BACKEND", "unknown")
    with pytest.raises(ValueError, match="ORCH_QUEUE_BACKEND"):
        build_wake_queue()


async def test_cloudflare_queue_closes_the_client_it_creates(monkeypatch):
    class AutoClient:
        def __init__(self):
            self.closed = False

        async def post(self, url, **_kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url))

        async def aclose(self):
            self.closed = True

    client = AutoClient()
    monkeypatch.setattr(
        wake_queue_module.httpx,
        "AsyncClient",
        lambda: client,
    )

    await CloudflareWakeQueue("https://queue.test", "token").publish(
        topic="run.queued",
        message_key="job-1",
        payload={},
    )

    assert client.closed is True


def test_wake_queue_factory_builds_cloudflare_and_sqs(monkeypatch):
    monkeypatch.setenv("ORCH_QUEUE_BACKEND", "cloudflare")
    monkeypatch.setenv("CF_QUEUE_PUSH_URL", "https://queue.test")
    monkeypatch.setenv("CF_API_TOKEN", "token")
    assert isinstance(build_wake_queue(), CloudflareWakeQueue)

    monkeypatch.setenv("ORCH_QUEUE_BACKEND", "sqs")
    monkeypatch.delenv("SQS_QUEUE_URL", raising=False)
    with pytest.raises(ValueError, match="SQS_QUEUE_URL"):
        build_wake_queue()

    fake_client = object()
    monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.test/queue")
    monkeypatch.setattr(
        wake_queue_module.boto3,
        "client",
        lambda service: fake_client if service == "sqs" else None,
    )
    queue = build_wake_queue()

    assert isinstance(queue, SqsWakeQueue)
    assert queue._client is fake_client


async def test_sqs_consumer_long_polls_and_acknowledges_only_after_processing():
    class FakeSqs:
        def __init__(self):
            self.responses = [
                {"Messages": [{"ReceiptHandle": "receipt-1"}]},
                {},
            ]
            self.calls = []

        def receive_message(self, **kwargs):
            self.calls.append(("receive", kwargs))
            return self.responses.pop(0)

        def delete_message(self, **kwargs):
            self.calls.append(("delete", kwargs))

    async def inline(function, **kwargs):
        return function(**kwargs)

    client = FakeSqs()
    consumer = SqsWakeConsumer(
        "https://sqs.test/queue",
        client=client,
        run_sync=inline,
    )

    receipt = await consumer.receive()
    await consumer.ack(receipt)

    assert receipt == "receipt-1"
    assert await consumer.receive() is None
    assert client.calls == [
        (
            "receive",
            {
                "QueueUrl": "https://sqs.test/queue",
                "MaxNumberOfMessages": 1,
                "WaitTimeSeconds": 20,
                "VisibilityTimeout": 180,
            },
        ),
        (
            "delete",
            {
                "QueueUrl": "https://sqs.test/queue",
                "ReceiptHandle": "receipt-1",
            },
        ),
        (
            "receive",
            {
                "QueueUrl": "https://sqs.test/queue",
                "MaxNumberOfMessages": 1,
                "WaitTimeSeconds": 20,
                "VisibilityTimeout": 180,
            },
        ),
    ]


def test_sqs_consumer_builds_its_client_from_the_aws_role(monkeypatch):
    client = object()
    monkeypatch.setattr(
        wake_queue_module.boto3,
        "client",
        lambda service: client if service == "sqs" else None,
    )

    consumer = SqsWakeConsumer("https://sqs.test/queue")

    assert consumer._client is client
