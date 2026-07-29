import { useCallback, useEffect, useReducer, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import {
  RUN_STATE_STALE_TIME_MS,
  cacheRunDetail,
  cacheRunSummary,
  invalidateRunQueries,
  queryKeys,
} from "./queries";
import type {
  Creator,
  EditableConcept,
  GateRef,
  Item,
  RunActivity,
  RunDetail,
  RunPhaseSnapshot,
  RunProgress,
  RunSummary,
  StreamEvent,
} from "../types";
import { sseUrl } from "./urls";

export type NodeState = { node: string; label: string; status: "running" | "done" };
export type RunPhase = RunPhaseSnapshot;
export type LlmStageStream = { stage: string; text: string; active: boolean };

export interface RunStreamState {
  phase: RunPhase;
  nodes: NodeState[];
  items: Record<string, Item>;
  creators: Record<string, Creator>;
  editConcepts: EditableConcept[];
  awaiting: Creator[];
  review: { concepts: EditableConcept[]; creators: Creator[] } | null;
  gate: GateRef | null;
  summary: RunSummary | null;
  progress: RunProgress | null;
  activity: RunActivity[];
  llm: string;
  llmByStage: Record<string, LlmStageStream>;
  log: { kind: string; text: string; ts: number }[];
  error: string | null;
}

const initial: RunStreamState = {
  phase: "idle",
  nodes: [],
  items: {},
  creators: {},
  editConcepts: [],
  awaiting: [],
  review: null,
  gate: null,
  summary: null,
  progress: null,
  activity: [],
  llm: "",
  llmByStage: {},
  log: [],
  error: null,
};

function log(s: RunStreamState, kind: string, text: string): RunStreamState["log"] {
  return [...s.log, { kind, text, ts: Date.now() }].slice(-200);
}

function keyedItems(items: Item[]): Record<string, Item> {
  return items.reduce<Record<string, Item>>((acc, item) => {
    acc[item.id] = item;
    return acc;
  }, {});
}

function mergeById<T extends { id: string }>(hydrated: T[], live: T[]): T[] {
  const byId = new Map<string, T>();
  hydrated.forEach((item) => byId.set(item.id, item));
  live.forEach((item) => byId.set(item.id, item));
  return [...byId.values()];
}

function mergeActivity(hydrated: RunActivity[], live: RunActivity[]): RunActivity[] {
  const byId = new Map<string, RunActivity>();
  hydrated.forEach((event) => byId.set(event.event_id, event));
  live.forEach((event) => byId.set(event.event_id, event));
  return [...byId.values()].slice(-100);
}

function newerProgress(current: RunProgress | null, hydrated?: RunProgress): RunProgress | null {
  if (!hydrated) return current;
  if (!current?.updated_at || !hydrated.updated_at) return hydrated;
  return Date.parse(current.updated_at) > Date.parse(hydrated.updated_at) ? current : hydrated;
}

function progressActivity(ev: Extract<StreamEvent, { type: "progress_event" }>): RunActivity {
  const suffix = {
    started: "started",
    progress: "in progress",
    completed: "completed",
    waiting: "is waiting",
    retrying: "is retrying",
    failed: "failed",
  }[ev.status];
  return {
    event_id: ev.event_id ?? `${ev.operation_id}:${ev.status}`,
    kind: "stage",
    status: ev.status,
    label: `${ev.stage_label} ${suffix}`,
    occurred_at: ev.occurred_at ?? null,
    stage_id: ev.stage_id,
    item_id: ev.item_id ?? null,
    attempt: ev.attempt ?? null,
    detail: null,
  };
}

function applyProgressEvent(
  progress: RunProgress | null,
  ev: Extract<StreamEvent, { type: "progress_event" }>,
): RunProgress | null {
  if (!progress) return null;
  const stages = progress.stages.map((stage) => {
    if (stage.id !== ev.stage_id) return stage;
    if (
      ev.status === "progress" &&
      typeof ev.completed_units === "number" &&
      typeof ev.total_units === "number"
    ) {
      return {
        ...stage,
        status: ev.completed_units >= ev.total_units ? "completed" as const : "running" as const,
        active_units: ev.completed_units >= ev.total_units ? 0 : 1,
        completed_units: ev.completed_units,
        total_units: ev.total_units,
        updated_at: ev.occurred_at ?? stage.updated_at,
      };
    }
    const activeDelta = ev.status === "started" ? 1 : ev.status === "completed" || ev.status === "failed" ? -1 : 0;
    const activeUnits = Math.max(0, stage.active_units + activeDelta);
    const completedUnits =
      ev.status === "completed"
        ? Math.min(stage.total_units || Number.MAX_SAFE_INTEGER, stage.completed_units + 1)
        : stage.completed_units;
    const status =
      ev.status === "failed"
        ? "failed"
        : ev.status === "waiting"
          ? "waiting"
          : activeUnits > 0
            ? "running"
            : completedUnits >= stage.total_units && stage.total_units > 0
              ? "completed"
              : stage.status;
    return {
      ...stage,
      status,
      active_units: activeUnits,
      completed_units: completedUnits,
      failed_units: ev.status === "failed" ? stage.failed_units + 1 : stage.failed_units,
      updated_at: ev.occurred_at ?? stage.updated_at,
    };
  });
  const activeStageIds = stages
    .filter((stage) => stage.parent_id !== null && (stage.status === "running" || stage.status === "waiting"))
    .map((stage) => stage.id);
  const fallbackActive = stages
    .filter((stage) => stage.parent_id === null && (stage.status === "running" || stage.status === "waiting"))
    .map((stage) => stage.id);
  const items = ev.item_id
    ? [
        ...progress.items.filter((item) => item.item_id !== ev.item_id),
        {
          item_id: ev.item_id,
          stage_id: ev.stage_id,
          status: ev.status === "failed" ? "failed" as const : ev.status === "completed" && ev.stage_id === "production" ? "completed" as const : "running" as const,
          attempt: ev.attempt ?? 0,
          updated_at: ev.occurred_at ?? null,
        },
      ]
    : progress.items;
  return {
    ...progress,
    execution_status: ev.status === "failed" ? "failed" : "running",
    stages,
    items,
    active_stage_ids: activeStageIds.length ? activeStageIds : fallbackActive,
    updated_at: ev.occurred_at ?? progress.updated_at,
  };
}

function setProgressStage(
  progress: RunProgress | null,
  stageId: string,
  status: "waiting" | "failed",
  executionStatus: RunProgress["execution_status"],
  occurredAt?: string,
): RunProgress | null {
  if (!progress) return null;
  const stages = progress.stages.map((stage) =>
    stage.id === stageId
      ? {
          ...stage,
          status,
          active_units: 0,
          updated_at: occurredAt ?? stage.updated_at,
        }
      : stage
  );
  return {
    ...progress,
    execution_status: executionStatus,
    stages,
    active_stage_ids: [stageId],
    updated_at: occurredAt ?? progress.updated_at,
  };
}

function gateActivity(
  eventId: string | undefined,
  occurredAt: string | undefined,
  stageId: "scripts" | "creators",
): RunActivity {
  return {
    event_id: eventId ?? `gate:${stageId}:waiting`,
    kind: "gate",
    status: "waiting",
    label: stageId === "scripts" ? "Waiting for script review" : "Waiting for creator approval",
    occurred_at: occurredAt ?? null,
    stage_id: stageId,
    item_id: null,
    attempt: null,
    detail: null,
  };
}

function clearGateOnResume(s: RunStreamState): Partial<RunStreamState> {
  if (s.phase !== "editing" && s.phase !== "awaiting" && s.phase !== "review") return {};
  return { phase: "running", editConcepts: [], awaiting: [], review: null, gate: null };
}

function llmStage(ev: Record<string, unknown>): string {
  return typeof ev.stage === "string" && ev.stage.trim() ? ev.stage : "default";
}

function gateRef(ev: {
  gate_id?: unknown;
  version?: unknown;
  gate_type?: unknown;
}): GateRef | null {
  const gateId = ev.gate_id;
  const version = ev.version;
  const gateType = ev.gate_type;
  if (
    typeof gateId === "string" &&
    typeof version === "number" &&
    (gateType === "edit_concepts" ||
      gateType === "approve_creators" ||
      gateType === "review_creative_plan")
  ) {
    return { gate_id: gateId, version, gate_type: gateType };
  }
  return null;
}

function hydrate(s: RunStreamState, detail: RunDetail): RunStreamState {
  const hydratedItems = keyedItems(detail.items);
  const phase =
    s.phase === "idle" || (s.phase === "running" && detail.phase !== "idle")
      ? detail.phase
      : s.phase;
  return {
    ...s,
    phase,
    items: { ...hydratedItems, ...s.items },
    editConcepts: mergeById(detail.edit_concepts, s.editConcepts),
    awaiting: mergeById(detail.awaiting, s.awaiting),
    review: detail.review ?? s.review,
    gate: detail.gate ?? s.gate,
    summary: s.summary ?? detail.summary,
    progress: newerProgress(s.progress, detail.progress),
    activity: mergeActivity(detail.activity ?? [], s.activity),
    error: s.error ?? detail.error ?? null,
  };
}

export function reduceRunStreamEvent(s: RunStreamState, ev: StreamEvent): RunStreamState {
  switch (ev.type) {
    case "run_start":
      return {
        ...s,
        phase: "running",
        progress: s.progress ? { ...s.progress, execution_status: "running" } : null,
        log: log(s, "run", "pipeline started"),
      };
    case "node_start": {
      const nodes = s.nodes.some((n) => n.node === ev.node)
        ? s.nodes.map((n) => (n.node === ev.node ? { ...n, status: "running" as const } : n))
        : [...s.nodes, { node: ev.node, label: ev.label, status: "running" as const }];
      return { ...s, ...clearGateOnResume(s), nodes, log: log(s, "node", `▶ ${ev.label}`) };
    }
    case "node_end": {
      const nodes = s.nodes.map((n) =>
        n.node === ev.node ? { ...n, status: "done" as const } : n
      );
      return { ...s, nodes, log: log(s, "node", `✓ ${ev.label}`) };
    }
    case "progress_event": {
      const activity = progressActivity(ev);
      if (s.activity.some((entry) => entry.event_id === activity.event_id)) return s;
      return {
        ...s,
        progress: applyProgressEvent(s.progress, ev),
        activity: mergeActivity(s.activity, [activity]),
      };
    }
    case "item_update":
      return {
        ...s,
        ...clearGateOnResume(s),
        items: { ...s.items, [ev.item.id]: ev.item },
        log: log(s, "item", `item ${ev.item.id} · ${ev.label}`),
      };
    case "awaiting_concept_edit":
      {
        const activity = gateActivity(ev.event_id, ev.occurred_at, "scripts");
      return {
        ...s,
        phase: "editing",
        editConcepts: ev.concepts,
        gate: gateRef(ev),
        progress: setProgressStage(s.progress, "scripts", "waiting", "waiting_for_user", ev.occurred_at),
        activity: mergeActivity(s.activity, [activity]),
        log: log(s, "gate", "waiting for concept edits"),
      };
      }
    case "awaiting_approval":
      {
        const activity = gateActivity(ev.event_id, ev.occurred_at, "creators");
        return {
          ...s,
          phase: "awaiting",
          awaiting: ev.creators,
          gate: gateRef(ev),
          progress: setProgressStage(s.progress, "creators", "waiting", "waiting_for_user", ev.occurred_at),
          activity: mergeActivity(s.activity, [activity]),
        };
      }
    case "awaiting_review": {
      const activity: RunActivity = {
        event_id: ev.event_id ?? "gate:review:waiting",
        kind: "gate",
        status: "waiting",
        label: "Creative plan is waiting for review",
        occurred_at: ev.occurred_at ?? null,
        stage_id: "review",
        item_id: null,
        attempt: null,
        detail: null,
      };
      return {
        ...s,
        phase: "review",
        review: { concepts: ev.concepts, creators: ev.creators },
        gate: gateRef(ev),
        progress: setProgressStage(
          s.progress,
          "review",
          "waiting",
          "waiting_for_user",
          ev.occurred_at,
        ),
        activity: mergeActivity(s.activity, [activity]),
      };
    }
    case "creator_start":
      return { ...s, log: log(s, "creator", `generating ${ev.creator_id}`) };
    case "creator_ready": {
      const c = ev.creator;
      return { ...s, creators: { ...s.creators, [c.id]: c } };
    }
    case "creator_update": {
      const c = ev.creator;
      return {
        ...s,
        creators: { ...s.creators, [c.id]: c },
        awaiting: s.awaiting.map((a) => (a.id === c.id ? c : a)),
      };
    }
    case "llm_start": {
      const stage = llmStage(ev);
      // Zera o buffer do stage: em modo agent o mesmo stage gera mais de uma vez
      // (draft -> revisão), e sem o reset a 2ª geração grudaria na 1ª — o painel
      // mostraria dois JSONs concatenados. llm_start = "nova geração começando".
      return {
        ...s,
        llmByStage: {
          ...s.llmByStage,
          [stage]: { stage, text: "", active: true },
        },
      };
    }
    case "llm_token": {
      const stage = llmStage(ev);
      const token = typeof ev.token === "string" ? ev.token : "";
      const current = s.llmByStage[stage] ?? { stage, text: "", active: true };
      return {
        ...s,
        llm: s.llm + token,
        llmByStage: {
          ...s.llmByStage,
          [stage]: { ...current, text: current.text + token, active: true },
        },
      };
    }
    case "llm_end": {
      const stage = llmStage(ev);
      const current = s.llmByStage[stage] ?? { stage, text: "", active: false };
      return {
        ...s,
        llm: s.llm + "\n",
        llmByStage: {
          ...s.llmByStage,
          [stage]: { ...current, active: false },
        },
      };
    }
    case "run_end":
      return {
        ...s,
        phase: "done",
        summary: ev.summary,
        progress: s.progress ? { ...s.progress, execution_status: "completed", active_stage_ids: [] } : null,
        log: log(s, "run", "pipeline finished"),
      };
    case "error":
      return {
        ...s,
        phase: "error",
        error: ev.message,
        progress: s.progress ? { ...s.progress, execution_status: "failed", active_stage_ids: [] } : null,
        log: log(s, "error", ev.message),
      };
    default:
      return s;
  }
}

type Action =
  | { kind: "event"; ev: StreamEvent }
  | { kind: "hydrate"; detail: RunDetail }
  | { kind: "reset" };

function rootReducer(s: RunStreamState, a: Action): RunStreamState {
  if (a.kind === "reset") return initial;
  if (a.kind === "hydrate") return hydrate(s, a.detail);
  return reduceRunStreamEvent(s, a.ev);
}

/**
 * Subscribe to the run SSE stream and reduce events into UI-ready state.
 * Pass `null` to stay idle (e.g. before a run is created).
 */
export function useRunStream(runId: string | null) {
  const [state, dispatch] = useReducer(rootReducer, initial);
  const esRef = useRef<EventSource | null>(null);
  const progressRefreshRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    dispatch({ kind: "reset" });
    if (!runId) return;
    let cancelled = false;
    queryClient
      .fetchQuery({
        queryKey: queryKeys.runState(runId),
        queryFn: () => api.getRunState(runId),
        staleTime: RUN_STATE_STALE_TIME_MS,
      })
      .then((detail) => {
        cacheRunDetail(queryClient, detail);
        if (!cancelled) {
          dispatch({ kind: "hydrate", detail });
        }
      })
      .catch(() => {
        /* The checkpoint can lag run creation; SSE remains the source of truth. */
      });

    const es = new EventSource(sseUrl(`/api/stream/${encodeURIComponent(runId)}`));
    esRef.current = es;
    es.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as StreamEvent;
        if (ev.type === "stream_end") {
          es.close();
          return;
        }
        if (ev.type === "run_end") {
          cacheRunSummary(queryClient, ev.summary);
          queryClient.invalidateQueries({ queryKey: queryKeys.runs() });
          queryClient.invalidateQueries({ queryKey: queryKeys.runState(runId) });
        } else if (
          ev.type === "item_update" ||
          ev.type === "awaiting_concept_edit" ||
          ev.type === "awaiting_approval" ||
          ev.type === "awaiting_review" ||
          ev.type === "error"
        ) {
          invalidateRunQueries(queryClient, runId);
        } else if (ev.type === "progress_event") {
          if (progressRefreshRef.current) clearTimeout(progressRefreshRef.current);
          progressRefreshRef.current = setTimeout(() => {
            queryClient
              .fetchQuery({
                queryKey: queryKeys.runState(runId),
                queryFn: () => api.getRunState(runId),
                staleTime: 0,
              })
              .then((detail) => {
                cacheRunDetail(queryClient, detail);
                if (!cancelled) dispatch({ kind: "hydrate", detail });
              })
              .catch(() => {
                /* SSE continues to provide the immediate state. */
              });
          }, 200);
        } else if (ev.type === "creator_update" || ev.type === "creator_ready") {
          queryClient.invalidateQueries({ queryKey: queryKeys.creators() });
        }
        dispatch({ kind: "event", ev });
      } catch {
        /* ignore malformed frame */
      }
    };
    es.onerror = () => {
      /* browser auto-reconnects; server replays the buffer on reconnect */
    };
    return () => {
      cancelled = true;
      if (progressRefreshRef.current) clearTimeout(progressRefreshRef.current);
      es.close();
      esRef.current = null;
    };
  }, [queryClient, runId]);

  const close = useCallback(() => esRef.current?.close(), []);
  return { ...state, close };
}
