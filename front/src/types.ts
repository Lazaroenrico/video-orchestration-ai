// Public contracts exposed by the FastAPI backend (src/orchestrator/web/server.py).
// Kept in sync with the normalizer helpers there.

export type MediaType = "image" | "video" | "audio" | "reference";

export interface Artifact {
  kind: string;
  uri: string;
  media_type: MediaType;
  renderable: boolean;
}

export interface QC {
  passed: boolean;
  score: number | null;
  reasons: string[];
}

export interface Creator {
  id: string;
  image_uri?: string | null;
  voice_ref?: string | null;
  voice_preview_uri?: string | null;
  image?: string | null;
  voice?: string | null;
  angles: string[];
  // Present on recovered/history entries.
  run_id?: string;
  offer?: string | null;
  status?: string;
}

export interface Item {
  id: string;
  creator_ref?: string | null;
  concept?: Record<string, unknown> | null;
  script?: string | null;
  tier?: string | null;
  attempts: number;
  cost_usd: number;
  qc?: QC | null;
  artifacts: Artifact[];
  assembled?: Artifact | null;
  dropped: boolean;
}

// runner.summarize(...) — returned by GET /api/status/{run_id} and the run_end event.
export interface RunSummary {
  run_id: string | null;
  produced: number;
  approved: number;
  dropped: number;
  in_flight: number;
  total_attempts: number;
  total_cost_usd: number;
  cost_by_tier: Record<string, number>;
  winning_styles: unknown[];
}

export interface RunsIndex {
  runs: string[];
  active: string[];
}

export interface CreatorsIndex {
  creators: Creator[];
  store_path: string;
  exists: boolean;
}

export interface IntegrationsIndex {
  stages: Record<string, string>;
}

export interface PromptTemplate {
  id: string;
  kind: string;
  title: string;
  text: string;
  desc?: string;
}

export interface PromptsIndex {
  templates: PromptTemplate[];
  last_used: Record<string, string | null>;
  store_path: string;
  exists: boolean;
}

export interface StartRunBody {
  offer: string;
  batch?: number;
  platform?: string;
  creator_prompt?: string | null;
  video_prompt?: string | null;
  approve_creators?: boolean;
  edit_concepts?: boolean;
}

// --- SSE stream events (GET /api/stream/{run_id}) --------------------------- //

export interface EditableConcept {
  id: string;
  script?: string;
  [k: string]: unknown;
}

export type StreamEvent =
  | { type: "run_start"; run_id: string; offer: string; batch: number }
  | { type: "node_start"; node: string; label: string }
  | { type: "node_end"; node: string; label: string; item?: Partial<Item> }
  | { type: "item_update"; run_id: string; node: string; label: string; item: Item }
  | { type: "awaiting_concept_edit"; run_id: string; concepts: EditableConcept[] }
  | { type: "awaiting_approval"; creators: Creator[] }
  | { type: "creator_update"; run_id: string; creator: Creator }
  | { type: "creator_start"; creator_id: string }
  | { type: "creator_ready"; creator: Creator }
  | { type: "llm_start"; [k: string]: unknown }
  | { type: "llm_token"; token?: string; [k: string]: unknown }
  | { type: "llm_end"; [k: string]: unknown }
  | { type: "run_end"; run_id: string; summary: RunSummary }
  | { type: "error"; message: string }
  | { type: "stream_end" };
