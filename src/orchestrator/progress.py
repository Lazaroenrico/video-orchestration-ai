"""Read model público de progresso da pipeline.

O grafo continua sendo a fonte de execução. Este módulo traduz seu vocabulário de
nodes para estágios estáveis de produto, usados igualmente por REST, SSE e frontend.

Os metadados de topologia (``STAGES``, ``NODE_STAGE``, nodes que alimentam o read
model e terminais de estágio) são **derivados** de ``orchestrator.graph.topology``
(fonte única de verdade); nenhuma tabela é duplicada aqui.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from orchestrator.common.plain import to_plain as _plain
from orchestrator.graph import topology as _topology

DEFAULT_TOPOLOGY = _topology.DEFAULT_TOPOLOGY
PipelineTopology = _topology.PipelineTopology
STAGES = _topology.STAGES
NODE_STAGE = _topology.NODE_STAGE
_ITEM_NODES = _topology.ITEM_PROGRESS_NODES
_TERMINAL_NODE = _topology.TERMINAL_NODE

_STAGE_LABELS = {stage["id"]: stage["label"] for stage in STAGES}


class LangChainEventProjector:
    """Project only safe LangChain model lifecycle events into the SSE contract.

    Message bodies, reasoning, tool arguments, structured chunks and artifacts are
    intentionally ignored. Nested LangGraph/LangChain callbacks are deduplicated
    by run, stage and tags; usage is read only from the terminal event metadata.
    """

    _START = frozenset({"on_chat_model_start", "on_llm_start"})
    _STREAM = frozenset({"on_chat_model_stream", "on_llm_stream"})
    _END = frozenset({"on_chat_model_end", "on_llm_end"})

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, str, tuple[str, ...]]] = set()

    @staticmethod
    def _usage(output: Any) -> dict[str, Any] | None:
        """Extract only numeric usage from LangChain message/result objects."""
        if isinstance(output, dict):
            direct = output.get("usage_metadata")
            if isinstance(direct, dict):
                return direct
            for value in output.values():
                usage = LangChainEventProjector._usage(value)
                if usage is not None:
                    return usage
        direct = getattr(output, "usage_metadata", None)
        if isinstance(direct, dict):
            return direct
        generations = getattr(output, "generations", None)
        if isinstance(generations, Iterable):
            for generation_group in generations:
                group = (
                    generation_group
                    if isinstance(generation_group, Iterable)
                    else (generation_group,)
                )
                for generation in group:
                    usage = LangChainEventProjector._usage(
                        getattr(generation, "message", generation)
                    )
                    if usage is not None:
                        return usage
        return None

    @staticmethod
    def _plain_text(chunk: Any) -> str | None:
        if isinstance(chunk, str):
            return chunk or None
        if isinstance(chunk, dict):
            if set(chunk) != {"content"} or not isinstance(chunk["content"], str):
                return None
            return chunk["content"] or None
        content = getattr(chunk, "content", None)
        if not isinstance(content, str) or not content:
            return None
        if any(
            getattr(chunk, name, None)
            for name in ("tool_calls", "invalid_tool_calls", "tool_call_chunks")
        ):
            return None
        additional = getattr(chunk, "additional_kwargs", None)
        if isinstance(additional, dict) and any(
            key in additional for key in ("tool_calls", "reasoning", "structured_response")
        ):
            return None
        return content

    @staticmethod
    def _context(event: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        tags = event.get("tags") if isinstance(event.get("tags"), list) else []
        tags_tuple = tuple(sorted(str(tag) for tag in tags if isinstance(tag, str)))
        stage = str(metadata.get("stage") or metadata.get("langgraph_node") or "llm")
        run_id = str(event.get("run_id") or "")
        return run_id, stage, tags_tuple

    def translate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event_type = str(event.get("event") or "")
        if event_type not in self._START | self._STREAM | self._END:
            return None
        run_id, stage, tags = self._context(event)
        key = (run_id, stage, event_type, tags)
        if event_type in self._START | self._END and key in self._seen:
            return None
        if event_type in self._START | self._END:
            self._seen.add(key)
        if event_type in self._START:
            return {"type": "llm_start", "stage": stage}
        if event_type in self._END:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            output = data.get("output") if isinstance(data, dict) else None
            usage = self._usage(output)
            payload: dict[str, Any] = {"type": "llm_end", "stage": stage}
            if isinstance(usage, dict):
                payload["usage"] = {
                    key: value
                    for key, value in usage.items()
                    if key in {"input_tokens", "output_tokens", "total_tokens", "total_cost"}
                    and isinstance(value, (int, float))
                }
            return payload
        # Only plain text chunks are public. AIMessageChunk metadata/content blocks
        # are deliberately excluded to avoid leaking structured output or tool args.
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        chunk = data.get("chunk") if isinstance(data, dict) else None
        text = self._plain_text(chunk)
        if text is None:
            return None
        return {"type": "llm_token", "stage": stage, "token": text}


# Public alias used by the durable and local stream adapters.
LanguageEventProjector = LangChainEventProjector


def _is_terminal_node(
    stage_id: str, node: str, topology: PipelineTopology = DEFAULT_TOPOLOGY
) -> bool:
    if stage_id == "talking_head":
        return node in topology.talking_head_nodes
    return topology.terminal_node.get(stage_id) == node


def _item_fields(value: Any) -> tuple[str | None, int | None]:
    plain = _plain(value)
    if not isinstance(plain, dict):
        return None, None
    if plain.get("id"):
        return str(plain["id"]), int(plain.get("attempts") or 0)
    for key in ("input", "output", "item"):
        item_id, attempt = _item_fields(plain.get(key))
        if item_id:
            return item_id, attempt
    results = plain.get("results")
    if isinstance(results, list):
        for item in reversed(results):
            item_id, attempt = _item_fields(item)
            if item_id:
                return item_id, attempt
    return None, None


class ProgressEventTranslator:
    """Traduz o stream do LangGraph sem perder identidade entre start/end."""

    def __init__(self, topology: PipelineTopology = DEFAULT_TOPOLOGY) -> None:
        self.topology = topology
        self._items: dict[str, tuple[str, int]] = {}
        self._llm = LangChainEventProjector()

    def translate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        stage_labels = {stage["id"]: stage["label"] for stage in self.topology.stages}
        llm_event = self._llm.translate(event)
        if llm_event is not None:
            return llm_event
        event_type = str(event.get("event") or "")
        if event_type == "on_custom_event":
            if event.get("name") != "creative_progress":
                return None
            data = event.get("data")
            if not isinstance(data, dict):
                return None
            stage_id = str(data.get("stage_id") or "")
            if stage_id not in stage_labels:
                return None
            completed = data.get("completed_units")
            total = data.get("total_units")
            if (
                not isinstance(completed, int)
                or isinstance(completed, bool)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or completed < 0
                or total < 1
                or completed > total
            ):
                return None
            return {
                "type": "progress_event",
                "operation_id": str(event.get("run_id") or f"{stage_id}:progress"),
                "stage_id": stage_id,
                "stage_label": stage_labels[stage_id],
                "node": stage_id,
                "status": "progress",
                "completed_units": completed,
                "total_units": total,
            }
        if event_type not in {"on_chain_start", "on_chain_end"}:
            return None
        metadata = event.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        node = str(metadata.get("langgraph_node") or event.get("name") or "")
        stage_id = self.topology.node_stage.get(node)
        if stage_id is None:
            return None
        operation_id = str(event.get("run_id") or "")
        data = event.get("data")
        item_id, attempt = (
            _item_fields(data) if node in self.topology.item_progress_nodes else (None, None)
        )
        if item_id and operation_id:
            self._items[operation_id] = (item_id, attempt or 0)
        elif operation_id in self._items:
            item_id, attempt = self._items[operation_id]

        public = {
            "type": "progress_event",
            "operation_id": operation_id,
            "stage_id": stage_id,
            "stage_label": stage_labels[stage_id],
            "node": node,
            "status": "started" if event_type == "on_chain_start" else "completed",
        }
        if item_id:
            public["item_id"] = item_id
            public["attempt"] = int(attempt or 0)
        if event_type == "on_chain_end" and operation_id:
            self._items.pop(operation_id, None)
        return public


def build_activity(
    events: Iterable[dict[str, Any]],
    *,
    limit: int = 100,
    topology: PipelineTopology = DEFAULT_TOPOLOGY,
) -> list[dict[str, Any]]:
    """Converte eventos técnicos em uma timeline curta e legível."""
    event_list = [event for event in events if isinstance(event, dict)]
    stage_labels = {stage["id"]: stage["label"] for stage in topology.stages}
    has_progress_events = any(event.get("type") == "progress_event" for event in event_list)
    activity: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_transitions: set[tuple[str | None, str | None, int | None, str]] = set()

    for index, event in enumerate(event_list):
        event_type = str(event.get("type") or "")
        if has_progress_events and event_type in {"node_start", "node_end"}:
            continue
        if has_progress_events and event_type == "item_update":
            continue
        kind: str
        status: str
        label: str
        detail: str | None = None
        stage_id = str(event.get("stage_id") or "") or None
        item_id = str(event.get("item_id") or event.get("creator_id") or "") or None
        attempt = event.get("attempt")

        if event_type == "run_start":
            kind, status, label = "run", "started", "Pipeline started"
        elif event_type == "run_queued":
            kind, status, label = "run", "queued", "Run queued"
        elif event_type == "job_started":
            kind, status, label = "run", "started", "Worker started execution"
        elif event_type == "job_retry":
            kind, status, label = "run", "retrying", "Worker will retry the run"
            detail = str(event.get("error") or "") or None
        elif event_type == "progress_event":
            kind = "stage"
            status = str(event.get("status") or "running")
            node = str(event.get("node") or "")
            completed = event.get("completed_units")
            total = event.get("total_units")
            if status == "progress":
                if not isinstance(completed, int) or not isinstance(total, int):
                    continue
                label = (
                    f"{stage_labels.get(stage_id or '', stage_id or 'Stage')}: {completed}/{total}"
                )
                detail = f"{completed} of {total} units completed"
            else:
                if status == "completed" and not _is_terminal_node(stage_id or "", node, topology):
                    continue
                stage_label = str(
                    event.get("stage_label")
                    or stage_labels.get(stage_id or "")
                    or stage_id
                    or "Stage"
                )
                suffix = {
                    "started": "started",
                    "completed": "completed",
                    "waiting": "is waiting",
                    "retrying": "is retrying",
                    "failed": "failed",
                }.get(status, status)
                label = f"{stage_label} {suffix}"
            transition_key = (
                stage_id,
                item_id,
                int(attempt) if isinstance(attempt, (int, float)) else None,
                f"{status}:{completed}" if status == "progress" else status,
            )
            if transition_key in seen_transitions:
                continue
            seen_transitions.add(transition_key)
        elif event_type == "awaiting_concept_edit":
            kind, status, label = "gate", "waiting", "Waiting for script review"
            stage_id = "scripts"
        elif event_type == "awaiting_approval":
            kind, status, label = "gate", "waiting", "Waiting for creator approval"
            stage_id = "creator_previews"
        elif event_type == "awaiting_review":
            kind, status, label = "gate", "waiting", "Plano criativo aguardando revisão"
            stage_id = "review"
        elif event_type == "creator_start":
            kind, status, label = "item", "started", "Creator generation started"
        elif event_type == "creator_ready":
            creator = event.get("creator")
            creator = creator if isinstance(creator, dict) else {}
            item_id = str(creator.get("id") or "") or item_id
            kind, status, label = "item", "completed", "Creator ready"
        elif event_type == "item_update":
            kind, status = "item", "progress"
            label = str(event.get("label") or "Clip updated")
            item = event.get("item")
            if isinstance(item, dict):
                item_id = str(item.get("id") or "") or item_id
        elif event_type == "run_end":
            kind, status, label = "run", "completed", "Pipeline completed"
        elif event_type in {"error", "job_failed"}:
            kind, status, label = "error", "failed", "Pipeline stopped with an error"
            detail = str(event.get("message") or event.get("error") or "") or None
        elif event_type in {"node_start", "node_end"}:
            kind = "stage"
            status = "started" if event_type == "node_start" else "completed"
            node = str(event.get("node") or "")
            stage_id = topology.node_stage.get(node)
            node_label = str(event.get("label") or node or "Stage")
            label = f"{node_label} {status}"
        else:
            continue

        event_id = str(event.get("event_id") or f"event-{index + 1}")
        if event_id in seen_ids:
            continue
        seen_ids.add(event_id)
        activity.append(
            {
                "event_id": event_id,
                "kind": kind,
                "status": status,
                "label": label,
                "occurred_at": event.get("occurred_at"),
                "stage_id": stage_id,
                "item_id": item_id,
                "attempt": int(attempt) if isinstance(attempt, (int, float)) else None,
                "detail": detail,
            }
        )

    return activity[-max(0, limit) :]


def _execution_status(phase: str, events: list[dict[str, Any]]) -> str:
    if phase == "error":
        return "failed"
    if phase == "done":
        return "completed"
    if phase in {"editing", "awaiting", "review"}:
        return "waiting_for_user"
    if phase == "cancelled":
        return "cancelled"
    types = {str(event.get("type") or "") for event in events}
    if "run_queued" in types and "job_started" not in types:
        return "queued"
    return "running"


def build_progress(
    events: Iterable[dict[str, Any]],
    *,
    phase: str,
    items: Iterable[dict[str, Any]] = (),
    batch_size: int | None = None,
    topology: PipelineTopology = DEFAULT_TOPOLOGY,
) -> dict[str, Any]:
    """Projeta eventos públicos em um snapshot de progresso reidratável."""
    event_list = [event for event in events if isinstance(event, dict)]
    item_list = [item for item in items if isinstance(item, dict)]
    total_items = batch_size or len(item_list)
    if not total_items:
        for event in event_list:
            if event.get("type") == "run_start":
                total_items = int(event.get("batch") or 0)
                break

    stages = {
        definition["id"]: {
            **definition,
            "status": "pending",
            "completed_units": 0,
            "active_units": 0,
            "failed_units": 0,
            "total_units": (
                total_items
                if definition["id"]
                in {
                    "production",
                    "talking_head",
                    "product_demo",
                    "qc",
                    "assembly",
                }
                else 1
            ),
            "updated_at": None,
        }
        for definition in topology.stages
    }
    active_operations: dict[str, tuple[str, str | None]] = {}
    completed_units: dict[str, set[str]] = {stage_id: set() for stage_id in stages}
    item_progress: dict[str, dict[str, Any]] = {}

    for event in event_list:
        event_type = str(event.get("type") or "")
        if event_type not in {"node_start", "node_end", "progress_event"}:
            continue
        node = str(event.get("node") or "")
        stage_id = str(event.get("stage_id") or topology.node_stage.get(node) or "")
        if stage_id not in stages:
            continue
        status = str(event.get("status") or "")
        lifecycle = (
            status
            if event_type == "progress_event"
            else "started"
            if event_type == "node_start"
            else "completed"
        )
        item_id = str(event.get("item_id") or "") or None
        operation_id = str(event.get("operation_id") or f"{node}:{item_id or 'batch'}")
        updated_at = event.get("occurred_at")
        stage = stages[stage_id]
        stage["updated_at"] = updated_at or stage["updated_at"]

        if lifecycle == "progress":
            completed = event.get("completed_units")
            total = event.get("total_units")
            if not isinstance(completed, int) or not isinstance(total, int):
                continue
            stage["completed_units"] = completed
            stage["total_units"] = total
            stage["status"] = "completed" if completed >= total else "running"
        elif lifecycle in {"started", "running", "retrying"}:
            active_operations[operation_id] = (stage_id, item_id)
            stage["status"] = "running"
            if item_id:
                item_progress[item_id] = {
                    "item_id": item_id,
                    "stage_id": stage_id,
                    "status": "running",
                    "attempt": int(event.get("attempt") or 0),
                    "updated_at": updated_at,
                }
        elif lifecycle in {"waiting", "waiting_for_user"}:
            stage["status"] = "waiting"
        elif lifecycle in {"failed", "error"}:
            active_operations.pop(operation_id, None)
            stage["status"] = "failed"
            stage["failed_units"] += 1
        elif lifecycle in {"completed", "done"}:
            active_operations.pop(operation_id, None)
            terminal = topology.terminal_node.get(stage_id)
            if stage_id == "talking_head" or terminal == node:
                if item_id is None and stage["total_units"] > 1:
                    stage["completed_units"] = stage["total_units"]
                else:
                    unit = item_id or "batch"
                    completed_units[stage_id].add(unit)
                    stage["completed_units"] = len(completed_units[stage_id])
                if item_id:
                    item_progress[item_id] = {
                        "item_id": item_id,
                        "stage_id": stage_id,
                        "status": ("completed" if stage_id == "production" else "running"),
                        "attempt": int(event.get("attempt") or 0),
                        "updated_at": updated_at,
                    }

    for stage_id, stage in stages.items():
        active = {
            item_id or operation_id
            for operation_id, (active_stage, item_id) in active_operations.items()
            if active_stage == stage_id
        }
        stage["active_units"] = len(active)
        if stage["status"] == "failed":
            continue
        if active:
            stage["status"] = "running"
        elif stage["completed_units"] and (
            stage["total_units"] in {0, 1} or stage["completed_units"] >= stage["total_units"]
        ):
            stage["status"] = "completed"

    if event_list or phase != "idle":
        stages["setup"]["status"] = "completed"
        stages["setup"]["completed_units"] = 1
    if phase == "editing":
        stages["scripts"]["status"] = "waiting"
    elif phase == "awaiting":
        stages["creator_previews"]["status"] = "waiting"
    elif phase == "review":
        stages["review"]["status"] = "waiting"
    elif phase == "done":
        if item_list:

            def failure_stage(item: dict[str, Any]) -> str | None:
                failure = item.get("failure")
                if isinstance(failure, dict):
                    stage = str(failure.get("stage") or "").strip()
                    return stage or None
                return None

            for stage_id in (
                "setup",
                "concepts",
                "scripts",
                "creator_profiles",
                "creator_previews",
                "creative_plan",
                "review",
            ):
                stages[stage_id]["status"] = "completed"
                stages[stage_id]["completed_units"] = 1
                stages[stage_id]["active_units"] = 0
            talking_failed = [item for item in item_list if failure_stage(item) == "talking_head"]
            product_candidates = [item for item in item_list if item not in talking_failed]
            product_failed = [
                item for item in product_candidates if failure_stage(item) == "product_demo"
            ]
            for stage_id, candidates, failures in (
                ("talking_head", item_list, talking_failed),
                ("product_demo", product_candidates, product_failed),
            ):
                stages[stage_id]["status"] = "completed"
                stages[stage_id]["total_units"] = len(candidates)
                stages[stage_id]["completed_units"] = len(candidates) - len(failures)
                stages[stage_id]["failed_units"] = len(failures)
                stages[stage_id]["active_units"] = 0

            dropped = [item for item in item_list if item.get("dropped")]
            terminal = [
                item
                for item in item_list
                if item.get("assembled") or item.get("dropped") or item.get("error")
            ]
            pre_qc_failed = talking_failed + product_failed
            qc_candidates = [item for item in item_list if item not in pre_qc_failed]
            qc = stages["qc"]
            qc.update(
                {
                    "status": "completed",
                    "total_units": len(qc_candidates),
                    "completed_units": len(qc_candidates),
                    "active_units": 0,
                    "failed_units": len(dropped),
                }
            )
            assembly_candidates = [
                item
                for item in item_list
                if not item.get("dropped")
                and (not item.get("error") or failure_stage(item) in {None, "assembly"})
            ]
            assembled = [item for item in assembly_candidates if item.get("assembled")]
            assembly_failed = [item for item in assembly_candidates if item.get("error")]
            assembly = stages["assembly"]
            assembly.update(
                {
                    "status": (
                        "completed"
                        if len(assembled) + len(assembly_failed) >= len(assembly_candidates)
                        else "failed"
                    ),
                    "total_units": len(assembly_candidates),
                    "completed_units": len(assembled),
                    "active_units": 0,
                    "failed_units": len(assembly_failed),
                }
            )
            production = stages["production"]
            production.update(
                {
                    "status": "completed",
                    "total_units": len(item_list),
                    "completed_units": len(terminal),
                    "active_units": 0,
                    "failed_units": len([item for item in item_list if item.get("error")]),
                }
            )
            item_progress = {
                str(item["id"]): {
                    "item_id": str(item["id"]),
                    "stage_id": (
                        "qc"
                        if item.get("dropped")
                        else (failure_stage(item) if failure_stage(item) in stages else "assembly")
                    ),
                    "status": (
                        "dropped"
                        if item.get("dropped")
                        else "failed"
                        if item.get("error")
                        else "completed"
                    ),
                    "attempt": int(item.get("attempts") or 0),
                    "updated_at": None,
                }
                for item in item_list
                if item.get("id")
            }
        else:
            for stage in stages.values():
                stage["status"] = "completed"
                if stage["total_units"]:
                    stage["completed_units"] = stage["total_units"]
                stage["active_units"] = 0
    elif phase in {"error", "cancelled"}:
        active_ids = [
            stage_id
            for stage_id, stage in stages.items()
            if stage["status"] in {"running", "waiting"}
        ]
        for stage_id in active_ids:
            stages[stage_id]["status"] = "failed"
            stages[stage_id]["active_units"] = 0

    creative_children = [
        stages[stage_id]
        for stage_id in (
            "concepts",
            "scripts",
            "creator_profiles",
            "creator_previews",
        )
    ]
    creative_parent = stages["creative_plan"]
    if any(child["status"] == "failed" for child in creative_children):
        creative_parent["status"] = "failed"
    elif any(child["status"] == "running" for child in creative_children):
        creative_parent["status"] = "running"
    elif all(child["status"] == "completed" for child in creative_children):
        creative_parent["status"] = "completed"

    production_children = [stages[stage_id] for stage_id in ("talking_head", "product_demo", "qc")]
    parent = stages["production"]
    if any(child["status"] == "failed" for child in production_children):
        parent["status"] = "failed"
    elif any(child["status"] == "running" for child in production_children):
        parent["status"] = "running"
    elif production_children and all(
        child["status"] == "completed" for child in production_children
    ):
        parent["status"] = "completed"

    ordered = [stages[definition["id"]] for definition in topology.stages]
    active_stage_ids = [
        stage["id"]
        for stage in ordered
        if stage["parent_id"] is not None and stage["status"] in {"running", "waiting"}
    ]
    if not active_stage_ids:
        active_stage_ids = [
            stage["id"]
            for stage in ordered
            if stage["parent_id"] is None and stage["status"] in {"running", "waiting"}
        ]
    updated_at = next(
        (event.get("occurred_at") for event in reversed(event_list) if event.get("occurred_at")),
        None,
    )
    return {
        "execution_status": _execution_status(phase, event_list),
        "stages": ordered,
        "items": list(item_progress.values()),
        "active_stage_ids": active_stage_ids,
        "updated_at": updated_at,
    }
