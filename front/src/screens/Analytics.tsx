import { PageHeader } from "../components/PageHeader";
import { Card, SectionTitle } from "../components/Card";
import { StatTile } from "../components/ProgressBar";
import { Icon } from "../components/Icon";
import { Loading, ErrorState } from "../components/States";
import { useAsync } from "../api/useAsync";
import { api } from "../api/client";
import type { RunSummary } from "../types";
import { usd, num, pct } from "../lib/format";

async function loadAnalytics() {
  const idx = await api.getRuns();
  const statuses = await Promise.all(
    idx.runs.slice(0, 40).map((id) => api.getStatus(id).catch(() => null))
  );
  return statuses.filter((s): s is RunSummary => s !== null);
}

// The design shows a bar chart of conversions over time. We have no time-series
// backend, so we render a lightweight bars view of produced/approved per run.
function Bars({ summaries }: { summaries: RunSummary[] }) {
  const max = Math.max(1, ...summaries.map((s) => s.produced));
  const rows = summaries.slice(-12);
  return (
    <div className="flex h-48 items-end gap-2" role="img" aria-label="Produced and QC-approved videos across the latest runs">
      {rows.length === 0 && (
        <span className="font-body-md text-body-md text-on-surface-variant m-auto">
          No runs to chart yet.
        </span>
      )}
      {rows.map((s, i) => (
        <div key={i} className="flex-1 flex flex-col items-center gap-1 group">
          <div className="w-full flex flex-col justify-end h-full">
            <div
              className="w-full bg-primary rounded-t"
              style={{ height: `${pct(s.produced, max)}%` }}
              title={`${s.produced} produced`}
            />
            <div
              className="w-full bg-success-published rounded-t"
              style={{ height: `${pct(s.approved, max)}%` }}
              title={`${s.approved} approved`}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Analytics() {
  const { data, loading, error } = useAsync(loadAnalytics, []);
  if (loading) return <Loading />;
  if (error) return <ErrorState message={error} />;
  const summaries = data ?? [];

  const produced = summaries.reduce((a, s) => a + s.produced, 0);
  const approved = summaries.reduce((a, s) => a + s.approved, 0);
  const dropped = summaries.reduce((a, s) => a + s.dropped, 0);
  const cost = summaries.reduce((a, s) => a + s.total_cost_usd, 0);
  const cpv = produced ? cost / produced : 0;

  return (
    <div>
      <PageHeader title="Creative Performance" subtitle="Analytics aggregated across your orchestration runs." />

      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-gutter">
        <StatTile label="Videos Produced" value={num(produced)} />
        <StatTile label="QC Approved" value={num(approved)} hint={produced ? `${pct(approved, produced)}%` : undefined} />
        <StatTile label="Dropped (QC)" value={num(dropped)} hint="rejected" hintTone="error" />
        <StatTile label="Total Cost" value={usd(cost)} />
        <StatTile label="Cost / Video" value={usd(cpv)} />
      </div>

      <div className="grid grid-cols-12 gap-gutter">
        <div className="col-span-12 xl:col-span-8">
          <Card>
            <SectionTitle title="Latest runs" />
            <Bars summaries={summaries} />
            <div className="flex items-center gap-4 mt-4 font-label-sm text-label-sm text-on-surface-variant">
              <span className="flex items-center gap-1">
                <span className="w-3 h-3 rounded bg-primary" /> Produced
              </span>
              <span className="flex items-center gap-1">
                <span className="w-3 h-3 rounded bg-success-published" /> QC Approved
              </span>
            </div>
          </Card>
        </div>

        <div className="col-span-12 xl:col-span-4">
          <Card className="bg-ai-processing/5 border-ai-processing/20 h-full">
            <div className="flex items-center gap-2 mb-3 text-ai-processing">
              <Icon name="monitoring" />
              <span className="font-headline-md text-headline-md">Observed metrics</span>
            </div>
            <p className="font-body-md text-body-md text-on-surface-variant mb-3">
              {produced
                ? `Approval rate is ${pct(approved, produced)}% across ${summaries.length} runs, at ${usd(
                    cpv
                  )} per produced video.`
                : "Run campaigns to populate production metrics."}
            </p>
            <p className="font-label-sm text-label-sm text-on-surface-variant">
              Bars are ordered by the latest stored runs; run timestamps and distribution metrics are not available in this build.
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
