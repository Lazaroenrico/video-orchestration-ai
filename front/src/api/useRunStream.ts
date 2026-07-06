import { useCallback, useEffect, useReducer, useRef } from "react";
import type { Creator, EditableConcept, Item, RunSummary, StreamEvent } from "../types";

export type NodeState = { node: string; label: string; status: "running" | "done" };
export type RunPhase = "idle" | "running" | "editing" | "awaiting" | "done" | "error";

export interface RunStreamState {
  phase: RunPhase;
  nodes: NodeState[];
  items: Record<string, Item>;
  creators: Record<string, Creator>;
  editConcepts: EditableConcept[];
  awaiting: Creator[];
  summary: RunSummary | null;
  llm: string;
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
  log: [],
  error: null,
};

function log(s: RunStreamState, kind: string, text: string): RunStreamState["log"] {
  return [...s.log, { kind, text, ts: Date.now() }].slice(-200);
}

function reduce(s: RunStreamState, ev: StreamEvent): RunStreamState {
  switch (ev.type) {
    case "run_start":
      return { ...initial, phase: "running", log: log(s, "run", "pipeline started") };
    case "node_start": {
      const nodes = s.nodes.some((n) => n.node === ev.node)
        ? s.nodes.map((n) => (n.node === ev.node ? { ...n, status: "running" as const } : n))
        : [...s.nodes, { node: ev.node, label: ev.label, status: "running" as const }];
      return { ...s, nodes, log: log(s, "node", `▶ ${ev.label}`) };
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
    case "llm_token":
      return { ...s, llm: s.llm + (typeof ev.token === "string" ? ev.token : "") };
    case "llm_end":
      return { ...s, llm: s.llm + "\n" };
    case "run_end":
      return { ...s, phase: "done", summary: ev.summary, log: log(s, "run", "pipeline finished") };
    case "error":
      return { ...s, phase: "error", error: ev.message, log: log(s, "error", ev.message) };
    default:
      return s;
  }
}

type Action = { kind: "event"; ev: StreamEvent } | { kind: "reset" };

function rootReducer(s: RunStreamState, a: Action): RunStreamState {
  if (a.kind === "reset") return initial;
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
    const es = new EventSource(`/api/stream/${encodeURIComponent(runId)}`);
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
      es.close();
      esRef.current = null;
    };
  }, [runId]);

  const close = useCallback(() => esRef.current?.close(), []);
  return { ...state, close };
}
