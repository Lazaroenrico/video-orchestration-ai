import { Link } from "react-router";
import { Card, SectionTitle } from "../components/Card";
import { StatTile, ProgressBar } from "../components/ProgressBar";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { RetryCampaignButton } from "../components/RetryCampaignButton";
import { Loading, ErrorState } from "../components/States";
import { useDashboardData } from "../api/queries";
import { mediaUrl } from "../api/urls";
import { usd, num, pct, shortRun } from "../lib/format";

export function Dashboard() {
  const { data, loading, error } = useDashboardData();

  if (loading) return <Loading label="Loading production overview…" />;
  if (error) return <ErrorState message={error} />;
  if (!data) return null;

  const { runsIdx, creators, summaries, agg } = data;
  const activeRuns = new Set(runsIdx.active);
  const activeSummaries = summaries.filter((summary) => summary.run_id && activeRuns.has(summary.run_id));

  return (
    <div>
      <div className="mb-gutter">
        <h1 className="hm-page-title mb-2 text-primary">Production overview</h1>
        <p className="font-body-lg text-body-lg text-on-surface-variant flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full ${
              activeRuns.size ? "bg-ai-processing animate-pulse" : runsIdx.errored.length ? "bg-error" : "bg-success-published"
            }`}
          />
          {activeRuns.size
            ? `${activeRuns.size} run${activeRuns.size === 1 ? "" : "s"} in progress.`
            : runsIdx.errored.length
            ? `${runsIdx.errored.length} run${runsIdx.errored.length === 1 ? " requires" : "s require"} attention.`
            : "No live runs right now."}
        </p>
      </div>

      <div className="grid grid-cols-12 gap-gutter mb-gutter">
        <div className="col-span-12 xl:col-span-8 grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatTile label="Videos Produced" value={num(agg.produced)} />
          <StatTile
            label="QC Approved"
            value={num(agg.approved)}
            hint={agg.produced ? `${pct(agg.approved, agg.produced)}%` : undefined}
          />
          <StatTile label="QC Dropped" value={num(agg.dropped)} hint="QC" hintTone="error" />
          <StatTile label="Total Cost" value={usd(agg.cost)} />
        </div>

        <div className="col-span-12 xl:col-span-4">
          <Card className="h-full">
            <div className="flex items-center gap-2 mb-3 text-primary">
              <Icon name="monitoring" />
              <span className="font-headline-md text-headline-md">Run health</span>
            </div>
            <p className="font-body-md text-body-md text-on-surface-variant">
              {summaries.length
                ? `${runsIdx.active.length} live · ${runsIdx.errored.length} errored · ${runsIdx.cancelled.length} cancelled · ${summaries.length} runs available for analysis.`
                : "Start a campaign to populate this workspace with production data."}
            </p>
            {runsIdx.errored.length === 1 && (
              <RetryCampaignButton runId={runsIdx.errored[0]} className="mt-4" />
            )}
            {runsIdx.errored.length > 1 && (
              <Link to="/campaigns" className="mt-4 inline-flex">
                <Button variant="secondary" icon="error">
                  Review failed campaigns
                </Button>
              </Link>
            )}
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
            {activeSummaries.length === 0 && (
              <p className="font-body-md text-body-md text-on-surface-variant py-6 text-center">
                No live runs. Open Campaigns to inspect completed and paused work.
              </p>
            )}
            <div className="flex flex-col divide-y divide-surface-border">
              {activeSummaries.slice(0, 6).map((s) => {
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
