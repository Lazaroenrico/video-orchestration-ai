import type {
  CreatorsIndex,
  Creator,
  IntegrationsIndex,
  PromptsIndex,
  PromptTemplate,
  EditableConcept,
  RunDetail,
  RunSummary,
  RunsIndex,
  StartRunBody,
} from "./contracts";
import { apiUrl } from "./urls";

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
    throw new Error(`${res.status} ${detail}`);
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
  getCreators: () => req<CreatorsIndex>("/api/creators"),
  rerollVoice: (runId: string, creatorId: string) =>
    req<{ ok: boolean; creator: Creator }>(
      `/api/approve/${encodeURIComponent(runId)}/creators/${encodeURIComponent(
        creatorId
      )}/reroll-voice`,
      { method: "POST" }
    ),
  approve: (runId: string, approved: string[]) =>
    req<{ ok: boolean }>(`/api/approve/${encodeURIComponent(runId)}`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),
  submitConcepts: (runId: string, concepts: EditableConcept[]) =>
    req<{ ok: boolean; count: number }>(
      `/api/approve/${encodeURIComponent(runId)}/concepts`,
      {
        method: "POST",
        body: JSON.stringify({ concepts }),
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
