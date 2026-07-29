import { useNavigate } from "react-router";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { ProgressBar } from "../components/ProgressBar";
import { StatusPill, type Status } from "../components/StatusPill";
import { RetryCampaignButton } from "../components/RetryCampaignButton";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useCampaignRows } from "../api/queries";
import type { RunSummary } from "../types";
import { usd, num, pct, shortRun } from "../lib/format";

function rowStatus(
  active: boolean,
  errored: boolean,
  cancelled: boolean,
  s: RunSummary | null,
): { status: Status; label: string } {
  if (cancelled) return { status: "cancelled", label: "Cancelled" };
  if (errored) return { status: "failed", label: "Failed" };
  if (active) return { status: "generating", label: "Generating" };
  if (!s) return { status: "draft", label: "Draft" };
  if (s.in_flight > 0) return { status: "processing", label: "Processing" };
  if (s.dropped > 0 && s.approved === 0) return { status: "review", label: "QC Attention" };
  if (s.approved > 0 || s.dropped > 0) return { status: "done", label: "Completed" };
  return { status: "review", label: "Awaiting Results" };
}

export function Campaigns() {
  const navigate = useNavigate();
  const { data, loading, error } = useCampaignRows();

  return (
    <div>
      <PageHeader
        title="Campaigns"
        subtitle="Every orchestration run, with live state and completed outputs."
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
          <div className="hidden md:block">
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
                const st = rowStatus(r.active, r.errored, r.cancelled, s);
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
                    <td
                      className="px-6 py-4 text-right text-on-surface-variant"
                      onClick={(event) => event.stopPropagation()}
                    >
                      {r.errored ? (
                        <RetryCampaignButton runId={r.id} variant="secondary" />
                      ) : (
                        <span className="material-symbols-outlined">chevron_right</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
          <div className="grid gap-3 p-3 md:hidden">
            {data!.map((r) => {
              const s = r.summary;
              const total = s?.produced ?? 0;
              const done = s ? s.approved + s.dropped : 0;
              const st = rowStatus(r.active, r.errored, r.cancelled, s);
              return (
                <div
                  key={r.id}
                  className="rounded-lg border border-surface-border bg-surface-container-lowest p-4 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                >
                  <button
                    type="button"
                    onClick={() => navigate(`/campaigns/${r.id}`)}
                    className="block w-full text-left"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate font-body-md text-body-md font-medium text-primary">Campaign {shortRun(r.id)}</div>
                        <div className="mt-1 truncate font-mono text-label-sm text-on-surface-variant">{r.id}</div>
                      </div>
                      <StatusPill status={st.status} label={st.label} />
                    </div>
                    <div className="mt-4 flex items-center gap-3">
                      <ProgressBar value={pct(done, total || 1)} tone={r.active ? "processing" : "success"} />
                      <span className="w-14 shrink-0 text-right font-label-sm text-label-sm text-on-surface-variant">
                        {total ? `${done}/${total}` : "—"}
                      </span>
                    </div>
                    <div className="mt-3 font-body-md text-body-md text-on-surface-variant">{s ? usd(s.total_cost_usd) : "Cost pending"}</div>
                  </button>
                  {r.errored && (
                    <RetryCampaignButton runId={r.id} variant="secondary" className="mt-4" />
                  )}
                </div>
              );
            })}
          </div>
          <div className="px-6 py-3 font-label-sm text-label-sm text-on-surface-variant">
            Showing {data!.length} campaign{data!.length === 1 ? "" : "s"} · total{" "}
            {num(data!.reduce((a, r) => a + (r.summary?.produced ?? 0), 0))} videos produced
          </div>
        </Card>
      )}
    </div>
  );
}
