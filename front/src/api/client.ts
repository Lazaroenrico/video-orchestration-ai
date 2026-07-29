import type {
  CreatorsIndex,
  Creator,
  IntegrationsIndex,
  PromptsIndex,
  PromptTemplate,
  EditableConcept,
  GateRef,
  RetryRunResponse,
  RunDetail,
  RunSummary,
  RunsIndex,
  StartRunBody,
  StartRunV2Body,
} from "./contracts";
import { apiUrl } from "./urls";

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status} ${detail}`);
    this.name = "HttpError";
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(apiUrl(path), {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = (body && (body.detail ?? body.message)) || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new HttpError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  getRuns: () => req<RunsIndex>("/api/runs"),
  getRunState: (runId: string) =>
    req<RunDetail>(`/api/state/${encodeURIComponent(runId)}`),
  getStatus: (runId: string) =>
    req<RunSummary>(`/api/status/${encodeURIComponent(runId)}`),
  startRun: (body: StartRunBody) =>
    req<{ run_id: string }>("/api/run", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  startRunV2: (body: StartRunV2Body) =>
    req<{ run_id: string; job_id?: string }>("/api/v2/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  reviewRunV2: (
    runId: string,
    body: {
      action: "approve" | "regenerate";
      concepts?: EditableConcept[];
      creators?: Creator[];
      target?: "concepts" | "scripts" | "creators";
      ids?: string[];
      feedback?: string;
      gate_id?: string;
      version?: number;
      gate_type?: "review_creative_plan";
    },
  ) =>
    req<{ ok: boolean; job_id?: string }>(
      `/api/v2/runs/${encodeURIComponent(runId)}/review`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  retryRun: (runId: string) =>
    req<RetryRunResponse>(`/api/run/${encodeURIComponent(runId)}/retry`, {
      method: "POST",
    }),
  getCreators: () => req<CreatorsIndex>("/api/creators"),
  rerollVoice: (runId: string, creatorId: string) =>
    req<{ ok: boolean; creator: Creator }>(
      `/api/approve/${encodeURIComponent(runId)}/creators/${encodeURIComponent(
        creatorId
      )}/reroll-voice`,
      { method: "POST" }
    ),
  approve: (runId: string, approved: string[], gate?: GateRef | null) =>
    req<{ ok: boolean }>(`/api/approve/${encodeURIComponent(runId)}`, {
      method: "POST",
      body: JSON.stringify({
        approved,
        ...(gate ? { gate_id: gate.gate_id, version: gate.version } : {}),
      }),
    }),
  submitConcepts: (runId: string, concepts: EditableConcept[], gate?: GateRef | null) =>
    req<{ ok: boolean; count: number }>(
      `/api/approve/${encodeURIComponent(runId)}/concepts`,
      {
        method: "POST",
        body: JSON.stringify({
          concepts,
          ...(gate ? { gate_id: gate.gate_id, version: gate.version } : {}),
        }),
      }
    ),
  getIntegrations: () => req<IntegrationsIndex>("/api/integrations"),
  getPrompts: () => req<PromptsIndex>("/api/prompts"),
  savePrompt: (t: { kind: string; title: string; text: string; desc?: string }) =>
    req<{ ok: boolean; template: PromptTemplate }>("/api/prompts", {
      method: "POST",
      body: JSON.stringify(t),
    }),
  deletePrompt: (id: string) =>
    req<{ ok: boolean }>(`/api/prompts/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};
