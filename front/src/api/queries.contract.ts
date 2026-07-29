import type { Creator, PromptsIndex, RunSummary, RunsIndex } from "./contracts";
import {
  aggregateSummaries,
  campaignRowsFromIndex,
  queryKeys,
  type CampaignRow,
} from "./queries";

const runsIndex: RunsIndex = {
  runs: ["web-done", "web-active", "web-error"],
  active: ["web-active"],
  errored: ["web-error"],
  cancelled: [],
};

const summary: RunSummary = {
  run_id: "web-done",
  produced: 2,
  approved: 1,
  dropped: 1,
  in_flight: 0,
  total_attempts: 3,
  total_cost_usd: 0.42,
  cost_by_tier: { ltx: 0.42 },
  winning_styles: [],
};

const rows: CampaignRow[] = campaignRowsFromIndex(runsIndex, {
  "web-done": summary,
});
const totals = aggregateSummaries([summary]);

export const queryCacheContract = {
  rows,
  totals,
  keys: [
    queryKeys.runs(),
    queryKeys.runStatus("web-done"),
    queryKeys.runState("web-done"),
    queryKeys.creators(),
    queryKeys.prompts(),
    queryKeys.integrations(),
  ],
} satisfies {
  rows: CampaignRow[];
  totals: {
    produced: number;
    approved: number;
    dropped: number;
    cost: number;
  };
  keys: readonly (readonly unknown[])[];
};

export type QueryCacheContractData = {
  creators: Creator[];
  prompts: PromptsIndex;
};
