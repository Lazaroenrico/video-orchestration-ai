import { useEffect, useState } from "react";
import { PageHeader } from "../components/PageHeader";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { MediaThumb } from "../components/MediaThumb";
import { StatusPill, type Status } from "../components/StatusPill";
import { RunSelect } from "../components/RunSelect";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useRunSelection } from "../api/useRunSelection";
import { useRunStream } from "../api/useRunStream";
import type { Item } from "../types";
import { usd } from "../lib/format";

function itemStatus(it: Item): { status: Status; label: string } {
  if (it.error) return { status: "failed", label: "Assembly Failed" };
  if (it.dropped) return { status: "failed", label: "QC Failed" };
  if (it.qc?.passed) return { status: "approved", label: "QC Passed" };
  if (it.qc) return { status: "review", label: "Needs Review" };
  if (it.assembled) return { status: "done", label: "Ready" };
  return { status: "processing", label: "Processing" };
}

function bestArtifact(it: Item) {
  return it.assembled ?? it.artifacts.find((a) => a.media_type === "video") ?? it.artifacts[0] ?? null;
}

export function VideoReview() {
  const { runs, active, selected, setSelected, loading, error } = useRunSelection();
  const run = useRunStream(selected);
  const items = Object.values(run.items);
  const [pick, setPick] = useState<string | null>(null);

  useEffect(() => {
    if (!pick && items.length) setPick(items[0].id);
  }, [items, pick]);

  const current = items.find((i) => i.id === pick) ?? items[0] ?? null;

  return (
    <div>
      <PageHeader
        title="Video Review & QC"
        subtitle="Review generated content for quality assurance before publishing."
        actions={<RunSelect runs={runs} active={active} selected={selected} onChange={setSelected} />}
      />

      {loading && <Loading />}
      {error && <ErrorState message={error} />}
      {!loading && !error && runs.length === 0 && (
        <EmptyState icon="movie" title="No runs to review" hint="Generated videos land here after a campaign runs." />
      )}

      {!loading && !error && runs.length > 0 && (
        <div className="grid grid-cols-12 gap-gutter">
          {/* Player + QC */}
          <div className="col-span-12 lg:col-span-8">
            <Card padded={false} className="overflow-hidden">
              <div className="bg-black aspect-video flex items-center justify-center">
                {current && bestArtifact(current)?.renderable ? (
                  <MediaThumb artifact={bestArtifact(current)} className="w-full h-full !aspect-video !rounded-none" />
                ) : (
                  <div className="text-inverse-on-surface flex flex-col items-center gap-2">
                    <Icon name="movie" size={40} />
                    <span className="font-body-md text-body-md">
                      {current ? "No playable render yet" : "No clips in this run"}
                    </span>
                  </div>
                )}
              </div>
              {current && (
                <div className="p-4 flex items-center justify-between">
                  <div>
                    <div className="font-headline-md text-headline-md text-primary">{current.id}</div>
                    <div className="font-body-md text-body-md text-on-surface-variant">
                      {current.creator_ref ?? "—"} · {usd(current.cost_usd)}
                    </div>
                  </div>
                  <StatusPill {...itemStatus(current)} />
                </div>
              )}
              {current?.error && (
                <div className="mx-4 mb-4 flex items-start gap-2 p-3 rounded-lg bg-error-container font-body-md text-body-md text-on-error-container">
                  <Icon name="error" size={18} className="mt-0.5 shrink-0" />
                  <span>{current.error}</span>
                </div>
              )}
            </Card>
          </div>

          {/* QC report */}
          <div className="col-span-12 lg:col-span-4">
            <Card>
              <SectionTitle title="Automated QC Report" />
              {current?.qc ? (
                <>
                  <div className="flex items-baseline gap-2 mb-4">
                    <span className="font-display-lg text-display-lg text-primary">
                      {current.qc.score != null ? Math.round(current.qc.score * 100) : "—"}
                    </span>
                    <span className="font-label-md text-label-md text-on-surface-variant">
                      / 100 overall
                    </span>
                  </div>
                  <div
                    className={`flex items-center gap-2 mb-4 font-label-md text-label-md ${
                      current.qc.passed ? "text-success-published" : "text-error"
                    }`}
                  >
                    <Icon name={current.qc.passed ? "check_circle" : "cancel"} size={18} />
                    {current.qc.passed ? "Passed automated checks" : "Flagged by QC"}
                  </div>
                  <ul className="flex flex-col gap-2">
                    {current.qc.reasons.length === 0 && (
                      <li className="font-body-md text-body-md text-on-surface-variant">
                        No issues reported.
                      </li>
                    )}
                    {current.qc.reasons.map((r, i) => (
                      <li
                        key={i}
                        className="flex items-start gap-2 p-2 rounded-lg bg-surface-container-low font-body-md text-body-md text-on-surface-variant"
                      >
                        <Icon name="info" size={16} className="mt-0.5 text-warning-review" />
                        {r}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="font-body-md text-body-md text-on-surface-variant">
                  QC runs after a clip is assembled. Nothing to score yet.
                </p>
              )}
            </Card>
          </div>

          {/* Filmstrip */}
          <div className="col-span-12">
            <SectionTitle title={`Clips in run (${items.length})`} />
            <div className="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-6 gap-3">
              {items.map((it) => {
                const s = itemStatus(it);
                return (
                  <button
                    key={it.id}
                    onClick={() => setPick(it.id)}
                    className={`text-left rounded-lg overflow-hidden border ${
                      pick === it.id ? "border-primary ring-1 ring-primary" : "border-surface-border"
                    }`}
                  >
                    <MediaThumb artifact={bestArtifact(it)} className="!rounded-none" />
                    <div className="p-2 bg-surface-container-lowest">
                      <div className="font-label-sm text-label-sm text-primary truncate">{it.id}</div>
                      <StatusPill status={s.status} label={s.label} />
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
