import { useCallback, useEffect, useReducer, useRef } from "react";
import { api } from "./client";
import type {
  Creator,
  EditableConcept,
  Item,
  RunDetail,
  RunPhaseSnapshot,
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
  summary: RunSummary | null;
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
  summary: null,
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

function clearGateOnResume(s: RunStreamState): Partial<RunStreamState> {
  if (s.phase !== "editing" && s.phase !== "awaiting") return {};
  return { phase: "running", editConcepts: [], awaiting: [] };
}

function llmStage(ev: Record<string, unknown>): string {
  return typeof ev.stage === "string" && ev.stage.trim() ? ev.stage : "default";
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
    summary: s.summary ?? detail.summary,
    error: s.error ?? detail.error ?? null,
  };
}

function reduce(s: RunStreamState, ev: StreamEvent): RunStreamState {
  switch (ev.type) {
    case "run_start":
      return { ...initial, phase: "running", log: log(s, "run", "pipeline started") };
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
    case "item_update":
      return {
        ...s,
        ...clearGateOnResume(s),
        items: { ...s.items, [ev.item.id]: ev.item },
        log: log(s, "item", `item ${ev.item.id} · ${ev.label}`),
      };
    case "awaiting_concept_edit":
      return {
        ...s,
        phase: "editing",
        editConcepts: ev.concepts,
        log: log(s, "gate", "waiting for concept edits"),
      };
    case "awaiting_approval":
      return { ...s, phase: "awaiting", awaiting: ev.creators };
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
      const current = s.llmByStage[stage] ?? { stage, text: "", active: false };
      return {
        ...s,
        llmByStage: {
          ...s.llmByStage,
          [stage]: { ...current, active: true },
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
      return { ...s, phase: "done", summary: ev.summary, log: log(s, "run", "pipeline finished") };
    case "error":
      return { ...s, phase: "error", error: ev.message, log: log(s, "error", ev.message) };
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
  return reduce(s, a.ev);
}

/**
 * Subscribe to the run SSE stream and reduce events into UI-ready state.
 * Pass `null` to stay idle (e.g. before a run is created).
 */
export function useRunStream(runId: string | null) {
  const [state, dispatch] = useReducer(rootReducer, initial);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    dispatch({ kind: "reset" });
    if (!runId) return;
    let cancelled = false;
    api
      .getRunState(runId)
      .then((detail) => {
        if (!cancelled) dispatch({ kind: "hydrate", detail });
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
      es.close();
      esRef.current = null;
    };
  }, [runId]);

  const close = useCallback(() => esRef.current?.close(), []);
  return { ...state, close };
}
