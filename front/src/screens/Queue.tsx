import { useState } from "react";
import { useSearchParams } from "react-router";
import { PageHeader } from "../components/PageHeader";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { RunSelect } from "../components/RunSelect";
import { RetryCampaignButton } from "../components/RetryCampaignButton";
import { PipelineProgress } from "../components/PipelineProgress";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useRunSelection } from "../api/useRunSelection";
import { useRunStream } from "../api/useRunStream";

export function Queue() {
  const [searchParams] = useSearchParams();
  const { runs, active, selected, setSelected, loading, error } = useRunSelection(searchParams.get("run"));
  const run = useRunStream(selected);
  const [showTrace, setShowTrace] = useState(true);

  const jobs = run.nodes;

  return (
    <div>
      <PageHeader
        title="Operations Trace"
        subtitle="Real-time stage activity for the selected orchestration run."
        actions={
          <RunSelect runs={runs} active={active} selected={selected} onChange={setSelected} />
        }
      />

      {loading && <Loading />}
      {error && <ErrorState message={error} />}
      {!loading && !error && runs.length === 0 && (
        <EmptyState icon="hourglass_empty" title="No runs" hint="Start a campaign to populate the job queue." />
      )}

      {!loading && !error && runs.length > 0 && (
        <div className="grid grid-cols-12 gap-gutter">
          <div className="col-span-12 xl:col-span-8">
            <Card>
              <SectionTitle title="Stage activity" />
              {run.progress ? (
                <PipelineProgress progress={run.progress} />
              ) : jobs.length === 0 ? (
                <p className="font-body-md text-body-md text-on-surface-variant py-8 text-center">
                  {active.has(selected ?? "")
                    ? "Waiting for the pipeline to emit jobs…"
                    : "This run has finished — reattach to a live run to watch jobs stream."}
                </p>
              ) : null}
            </Card>
          </div>

          {/* Detail / error trace panel */}
          <div className="col-span-12 xl:col-span-4">
            <Card>
              <div className="flex items-center justify-between mb-4">
                <span className="font-headline-md text-headline-md text-primary">Runtime detail</span>
                <StatusPill
                  status={run.phase === "error" ? "failed" : run.phase === "done" ? "done" : "processing"}
                  label={run.phase}
                />
              </div>
              {run.error ? (
                <>
                  <button
                    onClick={() => setShowTrace((v) => !v)}
                    className="flex items-center gap-2 text-error font-label-md text-label-md mb-2"
                  >
                    <Icon name={showTrace ? "expand_less" : "expand_more"} size={18} /> Technical error detail
                  </button>
                  {showTrace && (
                    <pre className="bg-inverse-surface text-inverse-on-surface rounded-lg p-3 text-xs overflow-x-auto whitespace-pre-wrap font-mono">
                      {run.error}
                    </pre>
                  )}
                  {selected && run.phase === "error" && (
                    <RetryCampaignButton runId={selected} className="mt-4" />
                  )}
                </>
              ) : (
                <div className="flex flex-col gap-3 max-h-[420px] overflow-y-auto">
                  {[...run.activity].reverse().slice(0, 40).map((entry) => (
                    <div key={entry.event_id} className="font-mono text-label-sm text-label-sm text-on-surface-variant">
                      {entry.occurred_at && (
                        <span className="text-on-surface-variant/60">
                          {new Date(entry.occurred_at).toLocaleTimeString()}{" "}
                        </span>
                      )}
                      {entry.label}
                      {entry.item_id && <span className="text-on-surface-variant/60"> · {entry.item_id}</span>}
                    </div>
                  ))}
                  {run.activity.length === 0 && (
                    <p className="font-body-md text-body-md text-on-surface-variant">No events yet.</p>
                  )}
                </div>
              )}
              {jobs.length > 0 && (
                <div className="mt-4 border-t border-surface-border pt-4">
                  <button
                    type="button"
                    onClick={() => setShowTrace((value) => !value)}
                    className="flex min-h-11 items-center gap-2 font-label-md text-label-md text-on-surface-variant hover:text-primary"
                  >
                    <Icon name={showTrace ? "expand_less" : "expand_more"} size={18} />
                    Technical node trace
                  </button>
                  {showTrace && (
                    <div className="mt-2 flex max-h-64 flex-col gap-2 overflow-y-auto">
                      {jobs.map((node, index) => (
                        <div key={`${node.node}-${index}`} className="flex items-center gap-2 font-mono text-label-sm text-on-surface-variant">
                          <Icon
                            name={node.status === "done" ? "check" : "sync"}
                            size={14}
                            className={node.status === "done" ? "" : "animate-spin"}
                          />
                          <span>{node.node}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
