"""Integration tests for runtime contract persistence and resume validation."""
from __future__ import annotations

import copy

import pytest

from orchestrator import runner
from orchestrator.graph.builder import build_graph
from orchestrator.graph.checkpoint import open_checkpointer
from orchestrator.runtime_contract import (
    LegacyPaidResumeBlockedError,
    RuntimeContractMismatchError,
)

_MOCK_PROVIDERS = {
    "adapters": {
        "llm": "mock",
        "creator": "mock",
        "video": "mock",
        "qc": "mock",
        "assembly": "mock",
        "upscale": "mock",
    },
    "storage": {"backend": "local"},
}

_PAID_PROVIDERS = {
    "adapters": {
        "llm": "vercel_gateway_llm",
        "creator": "creator_vercel_elevenlabs_design",
        "video": "replicate",
        "qc": "integrity_qc",
        "assembly": "ffmpeg_assembly",
        "upscale": "passthrough_upscale",
    },
    "storage": {"backend": "r2"},
}


async def test_new_run_persists_runtime_contract_in_checkpoint(tmp_path, pipeline_cfg):
    db_path = tmp_path / "runs.sqlite"
    run_id, output = await runner.run_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id="run-rc-new",
        batch=2,
    )

    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline_cfg, checkpointer=cp)
        snap = await app.aget_state({"configurable": {"thread_id": run_id}})

    assert snap is not None
    assert "runtime_contract" in snap.values
    persisted_contract = snap.values["runtime_contract"]
    assert persisted_contract["graph_version"] == "v2"
    assert persisted_contract["schema_version"] == "creative-v2"
    assert "fingerprint" in persisted_contract
    assert "config_hash" in persisted_contract
    assert persisted_contract["provider_aliases"]["video"] == "mock"


async def test_resume_matching_fingerprint_succeeds(tmp_path, pipeline_cfg):
    db_path = tmp_path / "runs.sqlite"
    run_id, output = await runner.run_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id="run-rc-matching",
        batch=2,
    )

    # Resume with exact same configuration
    resumed_id, resumed_out = await runner.resume_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id=run_id,
    )
    assert resumed_id == run_id
    assert len(resumed_out["results"]) == 2


async def test_resume_mismatched_fingerprint_blocks_before_execution(tmp_path, pipeline_cfg):
    db_path = tmp_path / "runs.sqlite"
    run_id, output = await runner.run_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id="run-rc-mismatch",
        batch=2,
    )

    # Modify pipeline (e.g. tiers or batch or voice)
    modified_pipeline = copy.deepcopy(pipeline_cfg)
    modified_pipeline["tiers"] = [
        {"name": "ltx_modified", "model": "other-model", "cost_per_second": 0.05}
    ]

    with pytest.raises(RuntimeContractMismatchError, match="fingerprint mismatch"):
        await runner.resume_pipeline(
            modified_pipeline,
            _MOCK_PROVIDERS,
            db_path=db_path,
            run_id=run_id,
        )


async def test_resume_legacy_run_without_contract_in_mock_succeeds(tmp_path, pipeline_cfg):
    # Simulate a legacy run initialized without runtime_contract in its checkpoint
    db_path = tmp_path / "legacy.sqlite"
    run_id = "run-legacy-mock"

    cfg = runner._build_config(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        run_id=run_id,
        platform="tiktok",
    )
    init = {
        "run_id": run_id,
        "config": {"offer": "test", "batch_size": 2},
    }
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline_cfg, checkpointer=cp)
        await app.ainvoke(init, cfg)

        snap = await app.aget_state({"configurable": {"thread_id": run_id}})
        assert "runtime_contract" not in snap.values

    # Resume with mock providers should succeed (legacy compatibility)
    resumed_id, resumed_out = await runner.resume_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id=run_id,
    )
    assert resumed_id == run_id


async def test_resume_legacy_run_without_contract_in_paid_fails(tmp_path, pipeline_cfg):
    # Simulate a legacy run without runtime_contract in its checkpoint
    db_path = tmp_path / "legacy_paid.sqlite"
    run_id = "run-legacy-paid"

    cfg = runner._build_config(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        run_id=run_id,
        platform="tiktok",
    )
    init = {
        "run_id": run_id,
        "config": {"offer": "test", "batch_size": 2},
    }
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline_cfg, checkpointer=cp)
        await app.ainvoke(init, cfg)

    # Resume with paid providers must raise LegacyPaidResumeBlockedError
    with pytest.raises(LegacyPaidResumeBlockedError, match="Paid run without runtime contract"):
        await runner.resume_pipeline(
            pipeline_cfg,
            _PAID_PROVIDERS,
            db_path=db_path,
            run_id=run_id,
        )


async def test_review_gate_interrupt_preserves_contract_and_validates_on_resume(
    tmp_path,
    pipeline_cfg,
):
    db_path = tmp_path / "gate.sqlite"
    run_id = "run-gate-rc"

    # Start run with review_plan enabled
    run_id, out = await runner.run_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id=run_id,
        batch=2,
        run_options={"review_plan": True},
    )

    # Check interrupt is pending
    interrupt = await runner.get_interrupt(
        pipeline_cfg,
        db_path=db_path,
        run_id=run_id,
    )
    assert interrupt is not None
    assert interrupt["type"] == "review_creative_plan"

    # Verify runtime_contract is in checkpoint
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline_cfg, checkpointer=cp)
        snap = await app.aget_state({"configurable": {"thread_id": run_id}})
        assert "runtime_contract" in snap.values

    # Resume with mismatched config should fail before any node execution
    modified_pipeline = copy.deepcopy(pipeline_cfg)
    modified_pipeline["batch"]["max_concurrency"] = 99
    with pytest.raises(RuntimeContractMismatchError):
        await runner.resume_pipeline(
            modified_pipeline,
            _MOCK_PROVIDERS,
            db_path=db_path,
            run_id=run_id,
            resume_value={
                "action": "approve",
                "creators": [
                    {
                        "id": c["id"],
                        "selected_voice_candidate_id": c["voice_candidates"][0]["candidate_id"],
                    }
                    for c in interrupt["creators"]
                ],
            },
            run_options={"review_plan": True},
        )

    # Resume with matching contract succeeds
    resumed_id, resumed_out = await runner.resume_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id=run_id,
        resume_value={
            "action": "approve",
            "creators": [
                {
                    "id": c["id"],
                    "selected_voice_candidate_id": c["voice_candidates"][0]["candidate_id"],
                }
                for c in interrupt["creators"]
            ],
        },
        run_options={"review_plan": True},
    )
    assert resumed_id == run_id
    assert resumed_out["review_approved"] is True
    assert len(resumed_out["results"]) == 2


async def test_resume_blocks_when_effective_llm_model_changes(tmp_path, pipeline_cfg, monkeypatch):
    db_path = tmp_path / "runs.sqlite"
    run_id = "run-llm-change"

    # Start run with mock LLM model
    run_id, output = await runner.run_pipeline(
        pipeline_cfg,
        _MOCK_PROVIDERS,
        db_path=db_path,
        run_id=run_id,
        batch=2,
    )

    # Change LLM model via pipeline configuration
    modified_pipeline = copy.deepcopy(pipeline_cfg)
    modified_pipeline["llm_model"] = "anthropic/claude-3-5-sonnet"

    with pytest.raises(RuntimeContractMismatchError, match="fingerprint mismatch"):
        await runner.resume_pipeline(
            modified_pipeline,
            _MOCK_PROVIDERS,
            db_path=db_path,
            run_id=run_id,
        )


async def test_resume_blocks_when_creator_image_model_changes(tmp_path, pipeline_cfg, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")

    async def fake_generate_face(self, index, system_prompt=None, voice_profile=None):
        return {
            "primary": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            "angles": ["front"],
        }

    async def fake_design_voice_candidates(self, spec, preview_text=None):
        from orchestrator.adapters.mock import MockAdapter
        return await MockAdapter(tiers=pipeline_cfg.get("tiers", [])).design_voice_candidates(spec, preview_text=preview_text)

    monkeypatch.setattr(
        "orchestrator.adapters.openai_image.OpenAIImageAdapter.generate_face",
        fake_generate_face,
    )
    monkeypatch.setattr(
        "orchestrator.adapters.creator_real.RealCreatorAdapter.design_voice_candidates",
        fake_design_voice_candidates,
    )

    db_path = tmp_path / "runs.sqlite"
    run_id = "run-image-model-change"

    live_providers = {
        "adapters": {
            "llm": "mock",
            "creator": "creator_vercel_elevenlabs_design",
            "video": "mock",
            "qc": "mock",
            "assembly": "mock",
            "upscale": "mock",
        },
        "storage": {"backend": "local"},
    }

    # Start run with default creator image model (openai/gpt-image-2)
    run_id, output = await runner.run_pipeline(
        pipeline_cfg,
        live_providers,
        db_path=db_path,
        run_id=run_id,
        batch=2,
        run_options={"review_plan": True},
    )

    # Change creator image model via environment variable
    monkeypatch.setenv("AI_GATEWAY_OPENAI_MODEL", "openai/dall-e-3")
    with pytest.raises(RuntimeContractMismatchError, match="fingerprint mismatch"):
        await runner.resume_pipeline(
            pipeline_cfg,
            live_providers,
            db_path=db_path,
            run_id=run_id,
        )


async def test_config_mock_with_judge_gateway_not_treated_as_paid_on_legacy_resume(
    tmp_path, pipeline_cfg
):
    db_path = tmp_path / "mock_judge.sqlite"
    run_id = "run-legacy-mock-judge"

    mock_providers_with_judge = {
        "adapters": {
            "llm": "mock",
            "creator": "mock",
            "video": "mock",
            "qc": "mock",
            "assembly": "mock",
            "upscale": "mock",
            "judge": "gateway",
        },
        "storage": {"backend": "local"},
    }

    # Simulate legacy checkpoint without runtime_contract
    async with open_checkpointer(db_path) as cp:
        app = build_graph(pipeline_cfg, checkpointer=cp)
        cfg = runner._build_config(
            pipeline_cfg,
            mock_providers_with_judge,
            run_id=run_id,
            platform="reels",
            agent_catalog=None,
            artifact_repository=None,
        )
        init = {
            "run_id": run_id,
            "config": {"offer": "test", "batch_size": 2},
            "campaign": {"offer": "test", "audience": "General audience"},
        }
        await app.ainvoke(init, cfg)
        snap = await app.aget_state({"configurable": {"thread_id": run_id}})
        assert "runtime_contract" not in snap.values

    # Resume should succeed without LegacyPaidResumeBlockedError because judge is not an execution adapter
    resumed_id, resumed_out = await runner.resume_pipeline(
        pipeline_cfg,
        mock_providers_with_judge,
        db_path=db_path,
        run_id=run_id,
    )
    assert resumed_id == run_id
    assert len(resumed_out["results"]) == 6


