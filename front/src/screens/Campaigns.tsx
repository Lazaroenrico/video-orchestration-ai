import { useNavigate } from "react-router-dom";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { ProgressBar } from "../components/ProgressBar";
import { StatusPill, type Status } from "../components/StatusPill";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAsync } from "../api/useAsync";
import { api } from "../api/client";
import type { RunSummary } from "../types";
import { usd, num, pct, shortRun } from "../lib/format";

async function loadCampaigns() {
  const idx = await api.getRuns();
  const activeSet = new Set(idx.active);
  const erroredSet = new Set(idx.errored);
  const rows = await Promise.all(
    idx.runs.map(async (id) => {
      const s = await api.getStatus(id).catch(() => null);
      return { id, active: activeSet.has(id), errored: erroredSet.has(id), summary: s };
    })
  );
  // Include active/errored runs that have no checkpointed status yet.
  for (const id of [...idx.active, ...idx.errored]) {
    if (!rows.some((r) => r.id === id))
      rows.push({ id, active: activeSet.has(id), errored: erroredSet.has(id), summary: null });
  }
  return rows;
}

function rowStatus(
  active: boolean,
  errored: boolean,
  s: RunSummary | null,
): { status: Status; label: string } {
  if (errored) return { status: "failed", label: "Failed" };
  if (active) return { status: "generating", label: "Generating" };
  if (!s) return { status: "draft", label: "Draft" };
  if (s.dropped > 0 && s.approved === 0) return { status: "failed", label: "Failed" };
  if (s.approved > 0) return { status: "published", label: "Published" };
  return { status: "review", label: "In Review" };
}

export function Campaigns() {
  const navigate = useNavigate();
  const { data, loading, error } = useAsync(loadCampaigns, []);

  return (
    <div>
      <PageHeader
        title="Campaigns"
        subtitle="Every orchestration run, newest first."
        actions={<Button icon="add" onClick={() => navigate("/campaigns/new")}>New Campaign</Button>}
      />

      {loading && <Loading />}
      {error && <ErrorState message={error} />}
      {!loading && !error && (data?.length ?? 0) === 0 && (
        <EmptyState
          icon="campaign"
          title="No campaigns yet"
          hint="Launch your first orchestration run to see it here."
          action={<Button icon="add" onClick={() => navigate("/campaigns/new")}>New Campaign</Button>}
        />
      )}

      {!loading && !error && (data?.length ?? 0) > 0 && (
        <Card padded={false} className="overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-surface-border text-left">
                {["Campaign", "Status", "Progress", "Cost", ""].map((h) => (
                  <th
                    key={h}
                    className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant px-6 py-3"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-border">
              {data!.map((r) => {
                const s = r.summary;
                const total = s?.produced ?? 0;
                const done = s ? s.approved + s.dropped : 0;
                const st = rowStatus(r.active, r.errored, s);
                return (
                  <tr
                    key={r.id}
                    onClick={() => navigate(`/campaigns/${r.id}`)}
                    className="cursor-pointer hover:bg-surface-container-low"
                  >
                    <td className="px-6 py-4">
                      <div className="font-body-md text-body-md text-primary font-medium">
                        Campaign {shortRun(r.id)}
                      </div>
                      <div className="font-mono text-label-sm text-label-sm text-on-surface-variant">
                        {r.id}
                      </div>
                    </td>
                    <td className="px-6 py-4">
                      <StatusPill status={st.status} label={st.label} />
                    </td>
                    <td className="px-6 py-4 w-56">
                      <div className="flex items-center gap-2">
                        <ProgressBar value={pct(done, total || 1)} tone={r.active ? "processing" : "success"} />
                        <span className="font-label-sm text-label-sm text-on-surface-variant w-14 text-right">
                          {total ? `${done}/${total}` : "—"}
                        </span>
                      </div>
                    </td>
                    <td className="px-6 py-4 font-body-md text-body-md text-primary">
                      {s ? usd(s.total_cost_usd) : "—"}
                    </td>
                    <td className="px-6 py-4 text-right text-on-surface-variant">
                      <span className="material-symbols-outlined">chevron_right</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="px-6 py-3 font-label-sm text-label-sm text-on-surface-variant">
            Showing {data!.length} campaign{data!.length === 1 ? "" : "s"} · total{" "}
            {num(data!.reduce((a, r) => a + (r.summary?.produced ?? 0), 0))} videos produced
          </div>
        </Card>
      )}
    </div>
  );
}
