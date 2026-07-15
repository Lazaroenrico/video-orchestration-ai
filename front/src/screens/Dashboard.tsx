import { Link } from "react-router-dom";
import { Card, SectionTitle } from "../components/Card";
import { StatTile, ProgressBar } from "../components/ProgressBar";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { Loading, ErrorState } from "../components/States";
import { useAsync } from "../api/useAsync";
import { api } from "../api/client";
import { mediaUrl } from "../api/urls";
import type { RunSummary } from "../types";
import { usd, num, pct, shortRun } from "../lib/format";

async function loadDashboard() {
  const [runsIdx, creatorsIdx] = await Promise.all([api.getRuns(), api.getCreators()]);
  const ids = runsIdx.runs.slice(0, 24);
  const statuses = await Promise.all(
    ids.map((id) => api.getStatus(id).catch(() => null))
  );
  const summaries = statuses.filter((s): s is RunSummary => s !== null);
  const agg = summaries.reduce(
    (a, s) => ({
      produced: a.produced + s.produced,
      approved: a.approved + s.approved,
      dropped: a.dropped + s.dropped,
      cost: a.cost + s.total_cost_usd,
    }),
    { produced: 0, approved: 0, dropped: 0, cost: 0 }
  );
  return { runsIdx, creators: creatorsIdx.creators, summaries, agg };
}

export function Dashboard() {
  const { data, loading, error } = useAsync(loadDashboard, []);

  if (loading) return <Loading label="Loading production overview…" />;
  if (error) return <ErrorState message={error} />;
  if (!data) return null;

  const { runsIdx, creators, summaries, agg } = data;
  const activeRuns = runsIdx.active;

  return (
    <div>
      <div className="mb-gutter">
        <h1 className="font-headline-lg text-headline-lg text-primary mb-2">Welcome back.</h1>
        <p className="font-body-lg text-body-lg text-on-surface-variant flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full ${
              activeRuns.length ? "bg-ai-processing animate-pulse" : "bg-success-published"
            }`}
          />
          {activeRuns.length
            ? `${activeRuns.length} run${activeRuns.length === 1 ? "" : "s"} in progress.`
            : "Your production pipeline is healthy."}
        </p>
      </div>

      <div className="grid grid-cols-12 gap-gutter mb-gutter">
        <div className="col-span-12 xl:col-span-8 grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatTile label="Videos Produced" value={num(agg.produced)} />
          <StatTile
            label="Approved"
            value={num(agg.approved)}
            hint={agg.produced ? `${pct(agg.approved, agg.produced)}%` : undefined}
          />
          <StatTile label="Dropped" value={num(agg.dropped)} hint="QC" hintTone="error" />
          <StatTile label="Total Cost" value={usd(agg.cost)} />
        </div>

        <div className="col-span-12 xl:col-span-4">
          <Card className="h-full bg-ai-processing/5 border-ai-processing/20">
            <div className="flex items-center gap-2 mb-3 text-ai-processing">
              <Icon name="auto_awesome" />
              <span className="font-headline-md text-headline-md">AI Insights</span>
            </div>
            <p className="font-body-md text-body-md text-on-surface-variant">
              {summaries.length
                ? `Across ${summaries.length} tracked runs, approval rate is ${pct(
                    agg.approved,
                    agg.produced || 1
                  )}%. Keep hooks tight to lift QC pass-through.`
                : "Run a campaign to unlock performance insights."}
            </p>
          </Card>
        </div>
      </div>

      <div className="grid grid-cols-12 gap-gutter">
        {/* Active production */}
        <div className="col-span-12 xl:col-span-8">
          <Card>
            <SectionTitle
              title="Active Production"
              action={
                <Link to="/campaigns" className="font-label-md text-label-md text-secondary">
                  View all
                </Link>
              }
            />
            {runsIdx.runs.length === 0 && (
              <p className="font-body-md text-body-md text-on-surface-variant py-6 text-center">
                No runs yet. Start your first campaign.
              </p>
            )}
            <div className="flex flex-col divide-y divide-surface-border">
              {summaries.slice(0, 6).map((s) => {
                const total = s.produced || 1;
                const done = s.approved + s.dropped;
                return (
                  <Link
                    key={s.run_id}
                    to={`/campaigns/${s.run_id}`}
                    className="flex items-center gap-4 py-3 hover:bg-surface-container-low -mx-2 px-2 rounded-lg"
                  >
                    <div className="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-on-surface-variant">
                      <Icon name="movie" size={18} />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="font-body-md text-body-md text-primary truncate">
                        Run {shortRun(s.run_id)}
                      </div>
                      <ProgressBar value={pct(done, total)} tone="processing" className="mt-1.5" />
                    </div>
                    <span className="font-label-sm text-label-sm text-on-surface-variant">
                      {done}/{s.produced}
                    </span>
                  </Link>
                );
              })}
            </div>
          </Card>
        </div>

        {/* Recent outputs */}
        <div className="col-span-12 xl:col-span-4">
          <Card>
            <SectionTitle
              title="Recent Creators"
              action={
                <Link to="/creators" className="font-label-md text-label-md text-secondary">
                  View all
                </Link>
              }
            />
            <div className="grid grid-cols-2 gap-3">
              {creators.slice(0, 4).map((c) => (
                <Link
                  to="/creators"
                  key={`${c.run_id}-${c.id}`}
                  className="rounded-lg overflow-hidden border border-surface-border bg-surface-container aspect-square"
                >
                  {(c.image_uri || c.image) && (
                    <img
                      src={mediaUrl(c.image_uri || c.image || "")}
                      alt={c.id}
                      className="w-full h-full object-cover"
                    />
                  )}
                </Link>
              ))}
              {creators.length === 0 && (
                <p className="col-span-2 font-body-md text-body-md text-on-surface-variant text-center py-6">
                  No creators yet.
                </p>
              )}
            </div>
            <Link to="/campaigns/new" className="block mt-4">
              <Button icon="add" className="w-full">
                New Campaign
              </Button>
            </Link>
          </Card>
        </div>
      </div>
    </div>
  );
}
