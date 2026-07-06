import { useState } from "react";
import { PageHeader } from "../components/PageHeader";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { StatusPill, type Status } from "../components/StatusPill";
import { RunSelect } from "../components/RunSelect";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useRunSelection } from "../api/useRunSelection";
import { useRunStream } from "../api/useRunStream";

export function Queue() {
  const { runs, active, selected, setSelected, loading, error } = useRunSelection();
  const run = useRunStream(selected);
  const [showTrace, setShowTrace] = useState(true);

  const jobs = run.nodes;

  const jobStatus = (s: "running" | "done"): { status: Status; label: string } =>
    s === "done" ? { status: "done", label: "Completed" } : { status: "processing", label: "Processing" };

  return (
    <div>
      <PageHeader
        title="Job Queue Orchestrator"
        subtitle="Real-time orchestration pipeline status."
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
              <SectionTitle title="Active Jobs" />
              {jobs.length === 0 && (
                <p className="font-body-md text-body-md text-on-surface-variant py-8 text-center">
                  {active.has(selected ?? "")
                    ? "Waiting for the pipeline to emit jobs…"
                    : "This run has finished — reattach to a live run to watch jobs stream."}
                </p>
              )}
              <div className="flex flex-col divide-y divide-surface-border">
                {jobs.map((n, i) => {
                  const s = jobStatus(n.status);
                  return (
                    <div key={`${n.node}-${i}`} className="flex items-center gap-4 py-3">
                      <span className="font-mono text-label-sm text-label-sm text-on-surface-variant w-16">
                        #{String(i + 1).padStart(3, "0")}
                      </span>
                      <div className="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-on-surface-variant">
                        <Icon name={n.status === "done" ? "check" : "sync"} size={18} className={n.status === "done" ? "" : "animate-spin"} />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="font-label-md text-label-md text-primary">{n.label}</div>
                        <div className="font-body-md text-body-md text-on-surface-variant">{n.node}</div>
                      </div>
                      <StatusPill status={s.status} label={s.label} />
                    </div>
                  );
                })}
              </div>
            </Card>
          </div>

          {/* Detail / error trace panel */}
          <div className="col-span-12 xl:col-span-4">
            <Card>
              <div className="flex items-center justify-between mb-4">
                <span className="font-headline-md text-headline-md text-primary">Run Detail</span>
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
                    <Icon name={showTrace ? "expand_less" : "expand_more"} size={18} /> Error Trace
                  </button>
                  {showTrace && (
                    <pre className="bg-inverse-surface text-inverse-on-surface rounded-lg p-3 text-xs overflow-x-auto whitespace-pre-wrap font-mono">
                      {run.error}
                    </pre>
                  )}
                </>
              ) : (
                <div className="flex flex-col gap-3 max-h-[420px] overflow-y-auto">
                  {[...run.log].reverse().slice(0, 40).map((l, i) => (
                    <div key={i} className="font-mono text-label-sm text-label-sm text-on-surface-variant">
                      <span className="text-on-surface-variant/60">
                        {new Date(l.ts).toLocaleTimeString()}{" "}
                      </span>
                      {l.text}
                    </div>
                  ))}
                  {run.log.length === 0 && (
                    <p className="font-body-md text-body-md text-on-surface-variant">No events yet.</p>
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
