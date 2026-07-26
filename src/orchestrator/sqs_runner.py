"""Loop portátil do Runner ECS: SQS sinaliza, PostgreSQL reivindica."""
from __future__ import annotations

from itertools import count
import os
from typing import Any, Awaitable, Callable

import orchestrator.job_store as job_store
from orchestrator.wake_queue import (
    SqsWakeConsumer,
    SqsWakeQueue,
    publish_outbox_once,
)
from orchestrator.worker import run_worker_once


async def run_sqs_cycle(
    consumer: SqsWakeConsumer,
    *,
    worker_id: str,
    publish_outbox: Callable[[], Awaitable[bool]],
    process_job: Callable[..., Awaitable[bool]],
) -> dict[str, bool]:
    """Executa um ciclo; falha do job mantém a mensagem para retry/DLQ."""
    outbox_published = await publish_outbox()
    receipt_handle = await consumer.receive()
    job_processed = await process_job(worker_id=worker_id)
    if receipt_handle is not None:
        await consumer.ack(receipt_handle)
    return {
        "outbox_published": outbox_published,
        "signal_received": receipt_handle is not None,
        "job_processed": job_processed,
    }


def _production_dependencies(worker_id: str):
    queue_url = os.environ.get("SQS_QUEUE_URL", "")
    if not queue_url:
        raise ValueError("SQS_QUEUE_URL é obrigatória para o Runner ECS")
    consumer = SqsWakeConsumer(queue_url)
    queue = SqsWakeQueue(queue_url)

    async def _publish() -> bool:
        async with job_store.open_repository() as jobs:
            if jobs is None:
                return False
            return await publish_outbox_once(
                jobs,
                queue,
                worker_id=f"{worker_id}-outbox",
            )

    return consumer, _publish, run_worker_once


async def run_sqs_runner(
    *,
    worker_id: str,
    cycles: int | None = None,
    consumer: SqsWakeConsumer | None = None,
    publish_outbox: Callable[[], Awaitable[bool]] | None = None,
    process_job: Callable[..., Awaitable[bool]] | None = None,
) -> dict[str, bool]:
    """Roda continuamente em ECS; ``cycles`` limita a execução em testes/smoke."""
    if consumer is None or publish_outbox is None or process_job is None:
        consumer, publish_outbox, process_job = _production_dependencies(worker_id)
    iterations = range(cycles) if cycles is not None else count()
    result = {
        "outbox_published": False,
        "signal_received": False,
        "job_processed": False,
    }
    for _ in iterations:
        result = await run_sqs_cycle(
            consumer,
            worker_id=worker_id,
            publish_outbox=publish_outbox,
            process_job=process_job,
        )
    return result
