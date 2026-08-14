from orchestrator.progress import (
    LangChainEventProjector,
    ProgressEventTranslator,
    build_activity,
    build_progress,
)


def test_langchain_projector_deduplicates_lifecycle_and_reads_message_usage():
    from langchain_core.messages import AIMessage

    projector = LangChainEventProjector()
    start = {
        "event": "on_chat_model_start",
        "run_id": "model-1",
        "metadata": {"stage": "scripts"},
        "tags": ["nested"],
    }
    assert projector.translate(start) == {"type": "llm_start", "stage": "scripts"}
    assert projector.translate(dict(start)) is None

    end = {
        "event": "on_chat_model_end",
        "run_id": "model-1",
        "metadata": {"stage": "scripts"},
        "tags": ["nested"],
        "data": {"output": AIMessage(
            content="safe text",
            usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        )},
    }
    assert projector.translate(end) == {
        "type": "llm_end",
        "stage": "scripts",
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }
    assert projector.translate(dict(end)) is None


def test_langchain_projector_allows_only_plain_text_and_never_leaks_structured_chunks():
    from langchain_core.messages import AIMessageChunk

    projector = LangChainEventProjector()
    base = {
        "event": "on_chat_model_stream",
        "run_id": "model-2",
        "metadata": {"stage": "concepts"},
    }
    assert projector.translate({**base, "data": {"chunk": "hello"}}) == {
        "type": "llm_token", "stage": "concepts", "token": "hello"
    }
    assert projector.translate({
        **base,
        "data": {"chunk": AIMessageChunk(content=[{"type": "text", "text": "secret"}])},
    }) is None
    assert projector.translate({
        **base,
        "data": {"chunk": AIMessageChunk(content="visible", tool_calls=[{
            "name": "x", "args": {}, "id": "call-1", "type": "tool_call"
        }])},
    }) is None
    assert projector.translate({
        **base,
        "data": {"chunk": {"content": "structured", "tool_calls": [{"name": "x"}]}},
    }) is None


def test_langchain_projector_preserves_usage_for_distinct_model_attempts():
    from langchain_core.messages import AIMessageChunk

    projector = LangChainEventProjector()
    outputs = []
    for run_id, tokens in (("attempt-1", (1, 2)), ("attempt-2", (3, 4))):
        outputs.append(projector.translate({
            "event": "on_chat_model_end",
            "run_id": run_id,
            "metadata": {"stage": "scripts"},
            "data": {"output": AIMessageChunk(
                content="", usage_metadata={
                    "input_tokens": tokens[0],
                    "output_tokens": tokens[1],
                    "total_tokens": sum(tokens),
                }
            )},
        }))
    assert outputs == [
        {"type": "llm_end", "stage": "scripts", "usage": {
            "input_tokens": 1, "output_tokens": 2, "total_tokens": 3,
        }},
        {"type": "llm_end", "stage": "scripts", "usage": {
            "input_tokens": 3, "output_tokens": 4, "total_tokens": 7,
        }},
    ]


def test_langgraph_progress_event_keeps_item_identity_until_node_completion():
    translator = ProgressEventTranslator()

    started = translator.translate({
        "event": "on_chain_start",
        "run_id": "operation-ltx-a",
        "metadata": {"langgraph_node": "ltx"},
        "data": {"input": {"id": "clip-a", "attempts": 1}},
    })
    completed = translator.translate({
        "event": "on_chain_end",
        "run_id": "operation-ltx-a",
        "metadata": {"langgraph_node": "ltx"},
        "data": {"output": {"tier": "ltx"}},
    })

    assert started == {
        "type": "progress_event",
        "operation_id": "operation-ltx-a",
        "stage_id": "talking_head",
        "stage_label": "Talking-head",
        "node": "ltx",
        "status": "started",
        "item_id": "clip-a",
        "attempt": 1,
    }
    assert completed == {
        **started,
        "status": "completed",
    }


def test_custom_creative_progress_exposes_completed_and_total_units():
    translator = ProgressEventTranslator()

    event = translator.translate({
        "event": "on_custom_event",
        "name": "creative_progress",
        "run_id": "scripts-operation",
        "data": {
            "stage_id": "scripts",
            "completed_units": 4,
            "total_units": 12,
        },
    })

    assert event == {
        "type": "progress_event",
        "operation_id": "scripts-operation",
        "stage_id": "scripts",
        "stage_label": "Escrevendo roteiros",
        "node": "scripts",
        "status": "progress",
        "completed_units": 4,
        "total_units": 12,
    }


def test_custom_creative_progress_rejects_malformed_or_unknown_payloads():
    translator = ProgressEventTranslator()

    assert translator.translate(
        {"event": "on_custom_event", "name": "other", "data": {}}
    ) is None
    assert translator.translate(
        {
            "event": "on_custom_event",
            "name": "creative_progress",
            "data": "invalid",
        }
    ) is None
    assert translator.translate(
        {
            "event": "on_custom_event",
            "name": "creative_progress",
            "data": {
                "stage_id": "unknown",
                "completed_units": 1,
                "total_units": 1,
            },
        }
    ) is None
    assert translator.translate(
        {
            "event": "on_custom_event",
            "name": "creative_progress",
            "data": {
                "stage_id": "scripts",
                "completed_units": 2,
                "total_units": 1,
            },
        }
    ) is None


def test_progress_snapshot_applies_granular_script_counter():
    progress = build_progress(
        [
            {
                "type": "progress_event",
                "operation_id": "scripts-operation",
                "stage_id": "scripts",
                "node": "scripts",
                "status": "progress",
                "completed_units": 4,
                "total_units": 12,
            }
        ],
        phase="running",
        batch_size=12,
    )

    scripts = next(stage for stage in progress["stages"] if stage["id"] == "scripts")
    assert scripts["status"] == "running"
    assert scripts["completed_units"] == 4
    assert scripts["total_units"] == 12
    assert progress["active_stage_ids"] == ["scripts"]


def test_completed_progress_counts_dropped_clips_without_claiming_they_were_assembled():
    progress = build_progress(
        [{"type": "run_start", "batch": 2}],
        phase="done",
        items=[
            {"id": "clip-a", "assembled": {"uri": "mock://final"}, "dropped": False},
            {"id": "clip-b", "assembled": None, "dropped": True},
        ],
        batch_size=2,
    )

    stages = {stage["id"]: stage for stage in progress["stages"]}
    assert stages["production"]["completed_units"] == 2
    assert stages["qc"]["failed_units"] == 1
    assert stages["assembly"]["status"] == "completed"
    assert stages["assembly"]["completed_units"] == 1
    assert stages["assembly"]["total_units"] == 1
    assert progress["items"] == [
        {
            "item_id": "clip-a",
            "stage_id": "assembly",
            "status": "completed",
            "attempt": 0,
            "updated_at": None,
        },
        {
            "item_id": "clip-b",
            "stage_id": "qc",
            "status": "dropped",
            "attempt": 0,
            "updated_at": None,
        },
    ]


def test_completed_progress_attributes_structured_video_failure_to_its_real_stage():
    progress = build_progress(
        [],
        phase="done",
        items=[
            {
                "id": "clip-failed",
                "error": "video provider operation failed",
                "failure": {"stage": "talking_head", "type": "WriteTimeout"},
                "assembled": None,
                "dropped": False,
            },
            {
                "id": "clip-ok",
                "assembled": {"uri": "mock://final"},
                "dropped": False,
            },
        ],
        batch_size=2,
    )

    stages = {stage["id"]: stage for stage in progress["stages"]}
    items = {item["item_id"]: item for item in progress["items"]}
    assert stages["talking_head"]["failed_units"] == 1
    assert stages["assembly"]["total_units"] == 1
    assert stages["assembly"]["failed_units"] == 0
    assert items["clip-failed"]["stage_id"] == "talking_head"


def test_progress_translator_ignores_noise_and_recovers_process_item_output():
    translator = ProgressEventTranslator()

    assert translator.translate({"event": "on_llm_stream"}) is None
    assert translator.translate({
        "event": "on_chain_start",
        "metadata": "invalid",
        "name": "not-a-pipeline-node",
    }) is None
    completed = translator.translate({
        "event": "on_chain_end",
        "run_id": "process-a",
        "metadata": {"langgraph_node": "process_item"},
        "data": {
            "output": {
                "results": [
                    "invalid",
                    {"id": "clip-a", "attempts": 2},
                ]
            }
        },
    })
    feedback = translator.translate({
        "event": "on_chain_start",
        "run_id": "feedback-a",
        "metadata": {"langgraph_node": "feedback"},
        "data": {"input": {"results": [{"id": "clip-a"}]}},
    })

    assert completed is not None
    assert completed["item_id"] == "clip-a"
    assert completed["attempt"] == 2
    assert feedback is None


def test_activity_formats_operational_events_and_deduplicates_replay():
    activity = build_activity([
        {"type": "run_queued", "event_id": "1"},
        {"type": "job_started", "event_id": "2"},
        {"type": "job_retry", "event_id": "3", "attempt": 2, "error": "timeout"},
        {"type": "awaiting_approval", "event_id": "4"},
        {"type": "creator_start", "event_id": "5", "creator_id": "creator-a"},
        {"type": "creator_ready", "event_id": "6", "creator": {"id": "creator-a"}},
        {
            "type": "item_update",
            "event_id": "7",
            "label": "QC",
            "item": {"id": "clip-a"},
        },
        {"type": "unknown", "event_id": "8"},
        {"type": "run_queued", "event_id": "1"},
    ])

    assert [entry["label"] for entry in activity] == [
        "Run queued",
        "Worker started execution",
        "Worker will retry the run",
        "Waiting for creator approval",
        "Creator generation started",
        "Creator ready",
        "QC",
    ]
    assert activity[2]["detail"] == "timeout"
    assert activity[3]["stage_id"] == "creator_previews"
    assert activity[5]["item_id"] == "creator-a"
    assert activity[6]["item_id"] == "clip-a"


def test_activity_handles_legacy_concept_gate_and_ignores_invalid_progress_counts():
    activity = build_activity(
        [
            {
                "type": "progress_event",
                "event_id": "invalid-progress",
                "stage_id": "scripts",
                "status": "progress",
                "completed_units": "one",
                "total_units": 2,
            },
            {"type": "awaiting_concept_edit", "event_id": "legacy-gate"},
        ]
    )

    assert [(entry["event_id"], entry["stage_id"]) for entry in activity] == [
        ("legacy-gate", "scripts"),
    ]


def test_activity_collapses_internal_nodes_into_canonical_stage_transitions():
    activity = build_activity([
        {
            "type": "progress_event",
            "event_id": "1",
            "stage_id": "concepts",
            "stage_label": "Concepts",
            "node": "persona",
            "status": "started",
        },
        {
            "type": "progress_event",
            "event_id": "2",
            "stage_id": "concepts",
            "stage_label": "Concepts",
            "node": "persona",
            "status": "completed",
        },
        {
            "type": "progress_event",
            "event_id": "3",
            "stage_id": "concepts",
            "stage_label": "Concepts",
            "node": "concepts",
            "status": "started",
        },
        {
            "type": "progress_event",
            "event_id": "4",
            "stage_id": "concepts",
            "stage_label": "Concepts",
            "node": "concepts",
            "status": "completed",
        },
        {
            "type": "progress_event",
            "event_id": "5",
            "stage_id": "assembly",
            "stage_label": "Assembly & upscale",
            "node": "assembly",
            "status": "started",
            "item_id": "clip-a",
        },
        {
            "type": "progress_event",
            "event_id": "6",
            "stage_id": "assembly",
            "stage_label": "Assembly & upscale",
            "node": "assembly",
            "status": "completed",
            "item_id": "clip-a",
        },
        {
            "type": "progress_event",
            "event_id": "7",
            "stage_id": "assembly",
            "stage_label": "Assembly & upscale",
            "node": "upscale",
            "status": "started",
            "item_id": "clip-a",
        },
        {
            "type": "progress_event",
            "event_id": "8",
            "stage_id": "assembly",
            "stage_label": "Assembly & upscale",
            "node": "upscale",
            "status": "completed",
            "item_id": "clip-a",
        },
        {
            "type": "item_update",
            "event_id": "9",
            "label": "Upscale",
            "item": {"id": "clip-a"},
        },
    ])

    assert [(entry["event_id"], entry["label"]) for entry in activity] == [
        ("1", "Concepts started"),
        ("4", "Concepts completed"),
        ("5", "Assembly & upscale started"),
        ("8", "Assembly & upscale completed"),
    ]


def test_progress_surfaces_queue_wait_failure_and_parent_failure():
    queued = build_progress(
        [{"type": "run_queued"}, {"type": "progress_event", "stage_id": "unknown"}],
        phase="running",
    )
    waiting = build_progress(
        [{
            "type": "progress_event",
            "operation_id": "scripts-wait",
            "stage_id": "scripts",
            "node": "concept_review",
            "status": "waiting",
        }],
        phase="running",
    )
    failed = build_progress(
        [{
            "type": "progress_event",
            "operation_id": "qc-a",
            "stage_id": "qc",
            "node": "qc",
            "status": "failed",
            "item_id": "clip-a",
        }],
        phase="running",
        batch_size=1,
    )
    errored = build_progress(
        [{
            "type": "progress_event",
            "operation_id": "assembly-a",
            "stage_id": "assembly",
            "node": "assembly",
            "status": "started",
            "item_id": "clip-a",
        }],
        phase="error",
        batch_size=1,
    )

    assert queued["execution_status"] == "queued"
    assert waiting["active_stage_ids"] == ["scripts"]
    failed_stages = {stage["id"]: stage for stage in failed["stages"]}
    assert failed_stages["qc"]["status"] == "failed"
    assert failed_stages["production"]["status"] == "failed"
    errored_stages = {stage["id"]: stage for stage in errored["stages"]}
    assert errored["execution_status"] == "failed"
    assert errored_stages["assembly"]["status"] == "failed"
    assert errored_stages["assembly"]["active_units"] == 0


def test_cancelled_execution_and_failed_creative_child_propagate_to_public_progress():
    cancelled = build_progress([], phase="cancelled")
    creative_failed = build_progress(
        [
            {
                "type": "progress_event",
                "operation_id": "concepts-failed",
                "stage_id": "concepts",
                "node": "concepts",
                "status": "failed",
            },
            {
                "type": "progress_event",
                "operation_id": "malformed-counter",
                "stage_id": "scripts",
                "node": "scripts",
                "status": "progress",
                "completed_units": "one",
                "total_units": 2,
            },
        ],
        phase="running",
    )

    stages = {stage["id"]: stage for stage in creative_failed["stages"]}
    assert cancelled["execution_status"] == "cancelled"
    assert stages["concepts"]["status"] == "failed"
    assert stages["creative_plan"]["status"] == "failed"


def test_public_progress_has_five_objective_phases_and_visible_internal_work():
    progress = build_progress(
        [
            {"type": "run_start", "batch": 3},
            {
                "type": "progress_event",
                "operation_id": "scripts",
                "stage_id": "scripts",
                "node": "scripts",
                "status": "started",
            },
        ],
        phase="running",
        batch_size=3,
    )

    stages = progress["stages"]
    top_level = [stage["id"] for stage in stages if stage["parent_id"] is None]
    assert top_level == ["setup", "creative_plan", "review", "production", "assembly"]
    assert progress["active_stage_ids"] == ["scripts"]
    assert {stage["id"]: stage["label"] for stage in stages}["scripts"] == (
        "Escrevendo roteiros"
    )


def test_combined_review_is_the_only_v2_waiting_phase():
    progress = build_progress(
        [{"type": "awaiting_review", "event_id": "review-1"}],
        phase="review",
        batch_size=2,
    )

    stages = {stage["id"]: stage for stage in progress["stages"]}
    assert progress["execution_status"] == "waiting_for_user"
    assert stages["review"]["status"] == "waiting"
    assert progress["active_stage_ids"] == ["review"]
