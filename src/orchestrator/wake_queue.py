"""Publicação da outbox; a fila externa é somente um sinal de wake-up."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

import boto3
import httpx

from orchestrator.db import PostgresJobRepository


class WakeQueue(Protocol):
    async def publish(
        self,
        *,
        topic: str,
        message_key: str,
        payload: dict[str, Any],
    ) -> None: ...


class DatabaseWakeQueue:
    """Sem broker: runners acordam por sweep periódico no PostgreSQL."""

    async def publish(
        self,
        *,
        topic: str,
        message_key: str,
        payload: dict[str, Any],
    ) -> None:
        return None


class CloudflareWakeQueue:
    def __init__(
        self,
        push_url: str,
        api_token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._push_url = push_url
        self._api_token = api_token
        self._client = client

    async def publish(
        self,
        *,
        topic: str,
        message_key: str,
        payload: dict[str, Any],
    ) -> None:
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                self._push_url,
                headers={"Authorization": f"Bearer {self._api_token}"},
                json={
                    "body": {
                        "topic": topic,
                        "message_key": message_key,
                        "payload": payload,
                    },
                    "content_type": "json",
                },
            )
            response.raise_for_status()
        finally:
            if self._client is None:
                await client.aclose()


async def _run_in_thread(
    function: Callable[..., Any],
    **kwargs: Any,
) -> Any:
    return await asyncio.to_thread(function, **kwargs)


class SqsWakeQueue:
    def __init__(
        self,
        queue_url: str,
        *,
        client: Any = None,
        run_sync: Callable[..., Awaitable[Any]] = _run_in_thread,
    ) -> None:
        self._queue_url = queue_url
        self._client = client or boto3.client("sqs")
        self._run_sync = run_sync

    async def publish(
        self,
        *,
        topic: str,
        message_key: str,
        payload: dict[str, Any],
    ) -> None:
        body = json.dumps(
            {
                "topic": topic,
                "message_key": message_key,
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        await self._run_sync(
            self._client.send_message,
            QueueUrl=self._queue_url,
            MessageBody=body,
        )


class SqsWakeConsumer:
    """Consome somente o sinal; o conteúdo canônico continua no PostgreSQL."""

    def __init__(
        self,
        queue_url: str,
        *,
        client: Any = None,
        run_sync: Callable[..., Awaitable[Any]] = _run_in_thread,
    ) -> None:
        self._queue_url = queue_url
        self._client = client or boto3.client("sqs")
        self._run_sync = run_sync

    async def receive(self) -> str | None:
        response = await self._run_sync(
            self._client.receive_message,
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=180,
        )
        messages = response.get("Messages") or []
        return messages[0]["ReceiptHandle"] if messages else None

    async def ack(self, receipt_handle: str) -> None:
        await self._run_sync(
            self._client.delete_message,
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
        )


def build_wake_queue() -> WakeQueue:
    backend = os.environ.get("ORCH_QUEUE_BACKEND", "database").strip().lower()
    if backend == "database":
        return DatabaseWakeQueue()
    if backend == "cloudflare":
        push_url = os.environ.get("CF_QUEUE_PUSH_URL", "")
        api_token = os.environ.get("CF_API_TOKEN", "")
        if not push_url or not api_token:
            raise ValueError(
                "CF_QUEUE_PUSH_URL e CF_API_TOKEN são obrigatórias"
            )
        return CloudflareWakeQueue(push_url, api_token)
    if backend == "sqs":
        queue_url = os.environ.get("SQS_QUEUE_URL", "")
        if not queue_url:
            raise ValueError("SQS_QUEUE_URL é obrigatória")
        return SqsWakeQueue(queue_url)
    raise ValueError(f"ORCH_QUEUE_BACKEND inválido: {backend!r}")


async def publish_outbox_once(
    jobs: PostgresJobRepository,
    queue: WakeQueue,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    entries = await jobs.claim_outbox(worker_id, limit=1, now=now)
    if not entries:
        return False
    entry = entries[0]
    try:
        await queue.publish(
            topic=entry.topic,
            message_key=entry.message_key,
            payload=entry.payload,
        )
    except Exception as exc:
        await jobs.fail_outbox(
            entry.entry_id,
            worker_id=worker_id,
            error=str(exc),
            now=now,
        )
        raise
    await jobs.mark_outbox_published(
        entry.entry_id,
        worker_id=worker_id,
        now=now,
    )
    return True
