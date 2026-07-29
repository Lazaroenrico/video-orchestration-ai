import type { RunExecutionStatus, RunProgress, StageProgress } from "../types";
import { Icon } from "./Icon";
import { StatusPill, type Status } from "./StatusPill";

const STATUS_ICON: Record<StageProgress["status"], string> = {
  completed: "check_circle",
  running: "sync",
  waiting: "pause_circle",
  failed: "error",
  pending: "circle",
};

const STATUS_CLASS: Record<StageProgress["status"], string> = {
  completed: "text-success-published",
  running: "text-ai-processing",
  waiting: "text-warning-review",
  failed: "text-error",
  pending: "text-on-surface-variant/50",
};

const EXECUTION_PILL: Record<RunExecutionStatus, { status: Status; label: string }> = {
  queued: { status: "draft", label: "Queued" },
  running: { status: "processing", label: "Running" },
  waiting_for_user: { status: "review", label: "Waiting for you" },
  completed: { status: "done", label: "Completed" },
  failed: { status: "failed", label: "Failed" },
  cancelled: { status: "failed", label: "Cancelled" },
};

function units(stage: StageProgress): string | null {
  if (stage.total_units <= 1) return null;
  if (stage.id === "production") {
    return `${stage.completed_units} of ${stage.total_units} clips complete`;
  }
  return `${stage.completed_units}/${stage.total_units}`;
}

function activeLabel(stage: StageProgress): string {
  const itemStage =
    stage.id === "production" || stage.parent_id === "production";
  if (!itemStage) return `${stage.label} in progress`;
  const count = stage.active_units || 1;
  return `${count} ${count === 1 ? "clip" : "clips"} in ${stage.label}`;
}

function StageRow({ stage, nested = false }: { stage: StageProgress; nested?: boolean }) {
  const count = units(stage);
  return (
    <div
      role="listitem"
      className={`flex min-h-11 items-center gap-3 border-b border-surface-border/70 py-2 last:border-b-0 ${
        nested ? "ml-7" : ""
      }`}
      aria-label={`${stage.label}: ${stage.status}`}
    >
      <span className={`flex size-7 shrink-0 items-center justify-center ${STATUS_CLASS[stage.status]}`}>
        <Icon
          name={STATUS_ICON[stage.status]}
          size={18}
          className={stage.status === "running" ? "animate-spin" : ""}
        />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-label-md text-label-md text-primary">{stage.label}</span>
        {stage.status === "waiting" && (
          <span className="block font-label-sm text-label-sm text-warning-review">Waiting for your input</span>
        )}
      </span>
      {count && (
        <span className="shrink-0 font-mono text-label-sm text-on-surface-variant">{count}</span>
      )}
    </div>
  );
}

export function PipelineProgress({ progress }: { progress: RunProgress }) {
  const pill = EXECUTION_PILL[progress.execution_status];
  const topLevel = progress.stages.filter((stage) => stage.parent_id === null);
  const active = progress.active_stage_ids
    .map((id) => progress.stages.find((stage) => stage.id === id))
    .filter((stage): stage is StageProgress => Boolean(stage));

  return (
    <div>
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-label-sm text-label-sm uppercase text-on-surface-variant">Current work</p>
          {active.length > 0 ? (
            <div className="mt-2 flex flex-col gap-1" aria-live="polite">
              {active.map((stage) => (
                <p key={stage.id} className="font-body-md text-body-md text-primary">
                  {activeLabel(stage)}
                </p>
              ))}
            </div>
          ) : (
            <p className="mt-2 font-body-md text-body-md text-on-surface-variant">
              {progress.execution_status === "queued"
                ? "Waiting for a worker"
                : progress.execution_status === "completed"
                  ? "All orchestration work is complete"
                  : "No stage is active"}
            </p>
          )}
        </div>
        <StatusPill status={pill.status} label={pill.label} />
      </div>

      <div role="list">
        {topLevel.map((stage) => (
          <div key={stage.id}>
            <StageRow stage={stage} />
            {progress.stages.some((child) => child.parent_id === stage.id) && (
              <div role="list" className="border-l border-surface-border">
                {progress.stages
                  .filter((child) => child.parent_id === stage.id)
                  .map((child) => <StageRow key={child.id} stage={child} nested />)}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
