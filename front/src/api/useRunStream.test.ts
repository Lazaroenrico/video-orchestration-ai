import { describe, expect, it } from "vitest";
import type { RunStreamState } from "./useRunStream";
import { reduceRunStreamEvent } from "./useRunStream";

const state: RunStreamState = {
  phase: "running",
  nodes: [],
  items: {},
  creators: {},
  editConcepts: [],
  awaiting: [],
  review: null,
  gate: null,
  summary: null,
  llm: "",
  llmByStage: {},
  log: [],
  error: null,
  activity: [],
  progress: {
    execution_status: "running",
    active_stage_ids: ["creators"],
    updated_at: null,
    items: [],
    stages: [
      {
        id: "creators",
        label: "Creators & approval",
        parent_id: null,
        status: "running",
        completed_units: 0,
        active_units: 1,
        failed_units: 0,
        total_units: 1,
        updated_at: null,
      },
      {
        id: "talking_head",
        label: "Talking-head",
        parent_id: "parallel_execution",
        status: "pending",
        completed_units: 0,
        active_units: 0,
        failed_units: 0,
        total_units: 2,
        updated_at: null,
      },
    ],
  },
};

describe("run stream reducer", () => {
  it("moves from a human gate back to live execution without replay duplicates", () => {
    const waiting = reduceRunStreamEvent(state, {
      type: "awaiting_approval",
      run_id: "run-1",
      creators: [],
      event_id: "42",
      occurred_at: "2026-07-28T10:00:00+00:00",
    });

    expect(waiting.progress?.execution_status).toBe("waiting_for_user");
    expect(waiting.progress?.stages[0].status).toBe("waiting");
    expect(waiting.activity.map((entry) => entry.label)).toEqual([
      "Waiting for creator approval",
    ]);

    const resumed = reduceRunStreamEvent(waiting, {
      type: "progress_event",
      event_id: "43",
      occurred_at: "2026-07-28T10:01:00+00:00",
      operation_id: "video-a",
      stage_id: "talking_head",
      stage_label: "Talking-head",
      node: "ltx",
      status: "started",
      item_id: "clip-a",
      attempt: 1,
    });
    const replayed = reduceRunStreamEvent(resumed, {
      type: "progress_event",
      event_id: "43",
      occurred_at: "2026-07-28T10:01:00+00:00",
      operation_id: "video-a",
      stage_id: "talking_head",
      stage_label: "Talking-head",
      node: "ltx",
      status: "started",
      item_id: "clip-a",
      attempt: 1,
    });

    expect(resumed.progress?.execution_status).toBe("running");
    expect(resumed.progress?.active_stage_ids).toEqual(["talking_head"]);
    expect(replayed.progress?.stages[1].active_units).toBe(1);
    expect(replayed.activity).toHaveLength(2);
  });
});
