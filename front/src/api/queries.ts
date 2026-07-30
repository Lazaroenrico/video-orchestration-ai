import { useMemo } from "react";
import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api } from "./client";
import type {
  Creator,
  EditableConcept,
  GateRef,
  PromptsIndex,
  RunDetail,
  RunSummary,
  RunsIndex,
  StartRunBody,
  StartRunV2Body,
} from "./contracts";

const SECOND = 1_000;
const MINUTE = 60 * SECOND;

export const RUNS_STALE_TIME_MS = 5 * SECOND;
export const RUN_ACTIVE_REFETCH_MS = 5 * SECOND;
export const RUN_DONE_STALE_TIME_MS = 5 * MINUTE;
export const RUN_STATE_STALE_TIME_MS = 10 * SECOND;
export const SUPPORTING_DATA_STALE_TIME_MS = 5 * MINUTE;
export const INTEGRATIONS_STALE_TIME_MS = 60 * MINUTE;

export const queryKeys = {
  runs: () => ["runs"] as const,
  runStatus: (runId: string) => ["run", runId, "status"] as const,
  runState: (runId: string) => ["run", runId, "state"] as const,
  creators: () => ["creators"] as const,
  integrations: () => ["integrations"] as const,
  prompts: () => ["prompts"] as const,
};

export interface CampaignRow {
  id: string;
  active: boolean;
  errored: boolean;
  cancelled: boolean;
  summary: RunSummary | null;
}

export interface SummaryTotals {
  produced: number;
  approved: number;
  dropped: number;
  cost: number;
}

export interface DashboardData {
  runsIdx: RunsIndex;
  creators: Creator[];
  summaries: RunSummary[];
  agg: SummaryTotals;
}

export function aggregateSummaries(summaries: RunSummary[]): SummaryTotals {
  return summaries.reduce(
    (acc, summary) => ({
      produced: acc.produced + summary.produced,
      approved: acc.approved + summary.approved,
      dropped: acc.dropped + summary.dropped,
      cost: acc.cost + summary.total_cost_usd,
    }),
    { produced: 0, approved: 0, dropped: 0, cost: 0 }
  );
}

export function campaignRowsFromIndex(
  index: RunsIndex,
  summariesByRunId: Record<string, RunSummary | null>
): CampaignRow[] {
  const active = new Set(index.active);
  const errored = new Set(index.errored);
  const cancelled = new Set(index.cancelled);
  const rows = index.runs.map((id) => ({
    id,
    active: active.has(id),
    errored: errored.has(id),
    cancelled: cancelled.has(id),
    summary: summariesByRunId[id] ?? null,
  }));
  for (const id of [...index.active, ...index.errored, ...index.cancelled]) {
    if (!rows.some((row) => row.id === id)) {
      rows.push({
        id,
        active: active.has(id),
        errored: errored.has(id),
        cancelled: cancelled.has(id),
        summary: summariesByRunId[id] ?? null,
      });
    }
  }
  return rows;
}

export function errorMessage(error: unknown): string | null {
  if (!error) return null;
  return error instanceof Error ? error.message : String(error);
}

export function useRunsIndex() {
  return useQuery({
    queryKey: queryKeys.runs(),
    queryFn: api.getRuns,
    staleTime: RUNS_STALE_TIME_MS,
  });
}

export function useRunStatus(runId: string | null, active = false) {
  return useQuery({
    queryKey: runId ? queryKeys.runStatus(runId) : ["run", "missing", "status"],
    queryFn: () => api.getStatus(runId ?? ""),
    enabled: Boolean(runId),
    staleTime: active ? RUNS_STALE_TIME_MS : RUN_DONE_STALE_TIME_MS,
    refetchInterval: active ? RUN_ACTIVE_REFETCH_MS : false,
    retry: 1,
  });
}

export function useRunState(runId: string | null) {
  return useQuery({
    queryKey: runId ? queryKeys.runState(runId) : ["run", "missing", "state"],
    queryFn: () => api.getRunState(runId ?? ""),
    enabled: Boolean(runId),
    staleTime: RUN_STATE_STALE_TIME_MS,
    retry: 1,
  });
}

export function useCreators() {
  return useQuery({
    queryKey: queryKeys.creators(),
    queryFn: api.getCreators,
    staleTime: SUPPORTING_DATA_STALE_TIME_MS,
  });
}

export function useIntegrations() {
  return useQuery({
    queryKey: queryKeys.integrations(),
    queryFn: api.getIntegrations,
    staleTime: INTEGRATIONS_STALE_TIME_MS,
  });
}

export function usePrompts() {
  return useQuery({
    queryKey: queryKeys.prompts(),
    queryFn: api.getPrompts,
    staleTime: SUPPORTING_DATA_STALE_TIME_MS,
  });
}

export function useCampaignRows(limit?: number) {
  const runsQuery = useRunsIndex();
  const index = runsQuery.data ?? null;
  const activeIds = useMemo(() => new Set(index?.active ?? []), [index]);
  const ids = useMemo(
    () => (limit == null ? index?.runs ?? [] : (index?.runs ?? []).slice(0, limit)),
    [index, limit]
  );
  const statusQueries = useQueries({
    queries: ids.map((id) => ({
      queryKey: queryKeys.runStatus(id),
      queryFn: () => api.getStatus(id).catch(() => null),
      staleTime: activeIds.has(id) ? RUNS_STALE_TIME_MS : RUN_DONE_STALE_TIME_MS,
      refetchInterval: activeIds.has(id) ? RUN_ACTIVE_REFETCH_MS : false,
      retry: 1,
    })),
  });

  const summariesByRunId: Record<string, RunSummary | null> = {};
  ids.forEach((id, index_) => {
    summariesByRunId[id] = statusQueries[index_]?.data ?? null;
  });
  const rows = index ? campaignRowsFromIndex(index, summariesByRunId) : [];
  const summaries = rows
    .map((row) => row.summary)
    .filter((summary): summary is RunSummary => summary !== null);

  return {
    data: rows,
    runs: index,
    summaries,
    loading: runsQuery.isLoading,
    fetching: runsQuery.isFetching || statusQueries.some((query) => query.isFetching),
    error: errorMessage(runsQuery.error),
  };
}

export function useDashboardData() {
  const campaigns = useCampaignRows(24);
  const creators = useCreators();
  const summaries = campaigns.summaries;
  const data: DashboardData | null =
    campaigns.runs && creators.data
      ? {
          runsIdx: campaigns.runs,
          creators: creators.data.creators,
          summaries,
          agg: aggregateSummaries(summaries),
        }
      : null;

  return {
    data,
    loading: campaigns.loading || (creators.isLoading && !creators.data),
    fetching: campaigns.fetching || creators.isFetching,
    error: campaigns.error ?? errorMessage(creators.error),
  };
}

export function useAnalyticsData(limit = 40) {
  const campaigns = useCampaignRows(limit);
  return {
    data: campaigns.summaries,
    loading: campaigns.loading,
    fetching: campaigns.fetching,
    error: campaigns.error,
  };
}

export function invalidateRunQueries(client: QueryClient, runId: string): void {
  client.invalidateQueries({ queryKey: queryKeys.runs() });
  client.invalidateQueries({ queryKey: queryKeys.runStatus(runId) });
  client.invalidateQueries({ queryKey: queryKeys.runState(runId) });
}

export function cacheRunDetail(client: QueryClient, detail: RunDetail): void {
  client.setQueryData(queryKeys.runState(detail.run_id), detail);
  if (detail.summary?.run_id) {
    client.setQueryData(queryKeys.runStatus(detail.summary.run_id), detail.summary);
  }
}

export function cacheRunSummary(client: QueryClient, summary: RunSummary): void {
  if (summary.run_id) {
    client.setQueryData(queryKeys.runStatus(summary.run_id), summary);
  }
}

export function useStartRunMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: StartRunBody) => api.startRun(body),
    onSuccess: ({ run_id }) => {
      invalidateRunQueries(client, run_id);
    },
  });
}

export function useStartRunV2Mutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: StartRunV2Body) => api.startRunV2(body),
    onSuccess: ({ run_id }) => {
      invalidateRunQueries(client, run_id);
    },
  });
}

export function useReviewRunV2Mutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      runId,
      gate,
      ...body
    }: {
      runId: string;
      action: "approve" | "regenerate";
      concepts?: EditableConcept[];
      creators?: Creator[];
      target?: "concepts" | "scripts" | "creators" | "voices";
      ids?: string[];
      feedback?: string;
      gate?: GateRef | null;
    }) =>
      api.reviewRunV2(runId, {
        ...body,
        ...(gate
          ? {
              gate_id: gate.gate_id,
              version: gate.version,
              gate_type: "review_creative_plan" as const,
            }
          : {}),
      }),
    onSettled: (_result, _error, variables) => {
      invalidateRunQueries(client, variables.runId);
    },
  });
}

export function useRetryRunMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.retryRun(runId),
    onSuccess: (retried, sourceRunId) => {
      invalidateRunQueries(client, sourceRunId);
      invalidateRunQueries(client, retried.run_id);
    },
  });
}

export function useApproveMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      runId,
      approved,
      gate,
    }: {
      runId: string;
      approved: string[];
      gate?: GateRef | null;
    }) => api.approve(runId, approved, gate),
    onSuccess: (_result, variables) => {
      invalidateRunQueries(client, variables.runId);
    },
  });
}

export function useSubmitConceptsMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      runId,
      concepts,
      gate,
    }: {
      runId: string;
      concepts: EditableConcept[];
      gate?: GateRef | null;
    }) => api.submitConcepts(runId, concepts, gate),
    onSuccess: (_result, variables) => {
      invalidateRunQueries(client, variables.runId);
    },
  });
}

export function useRerollVoiceMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, creatorId }: { runId: string; creatorId: string }) =>
      api.rerollVoice(runId, creatorId),
    onSuccess: (_result, variables) => {
      invalidateRunQueries(client, variables.runId);
      client.invalidateQueries({ queryKey: queryKeys.creators() });
    },
  });
}

export function useSavePromptMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (template: { kind: string; title: string; text: string; desc?: string }) =>
      api.savePrompt(template),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: queryKeys.prompts() });
    },
  });
}

export function useDeletePromptMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deletePrompt(id),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: queryKeys.prompts() });
    },
  });
}

export function promptStoreFromCache(data: PromptsIndex | undefined) {
  return {
    storePath: data?.store_path,
    exists: data?.exists,
  };
}
