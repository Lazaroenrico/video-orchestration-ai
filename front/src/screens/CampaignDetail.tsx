import { useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { StatusPill, type Status } from "../components/StatusPill";
import { ProgressBar } from "../components/ProgressBar";
import { useRunStream, type RunPhase } from "../api/useRunStream";
import { api } from "../api/client";
import { mediaUrl } from "../api/urls";
import type { Creator, Item } from "../types";
import { shortRun, usd, pct } from "../lib/format";

// Canonical pipeline stages (grouped from the backend node vocabulary).
const STAGES: { key: string; label: string; nodes: string[] }[] = [
  { key: "roster", label: "Creators", nodes: ["roster", "approval"] },
  { key: "concepts", label: "Concepts", nodes: ["concepts"] },
  { key: "scripts", label: "Scripts", nodes: ["scripts"] },
  { key: "video", label: "Video", nodes: ["ltx", "kling", "seedance", "product_demo"] },
  { key: "qc", label: "QC", nodes: ["qc"] },
  { key: "assembly", label: "Assembly", nodes: ["assembly", "upscale"] },
];

function phasePill(phase: RunPhase): { status: Status; label: string } {
  switch (phase) {
    case "running":
      return { status: "generating", label: "Generating" };
    case "awaiting":
      return { status: "review", label: "Awaiting Approval" };
    case "editing":
      return { status: "review", label: "Review Scripts" };
    case "done":
      return { status: "published", label: "Completed" };
    case "error":
      return { status: "failed", label: "Error" };
    default:
      return { status: "draft", label: "Idle" };
  }
}

function itemStatus(it: Item): { status: Status; label: string } {
  if (it.error) return { status: "failed", label: "Assembly Failed" };
  if (it.dropped) return { status: "failed", label: "Failed QC" };
  if (it.assembled) return { status: "done", label: "Done" };
  if (it.qc) return { status: it.qc.passed ? "approved" : "review", label: it.qc.passed ? "QC Pass" : "QC Review" };
  if (it.script) return { status: "processing", label: "Rendering" };
  return { status: "generating", label: "Generating" };
}

function stageLabel(stage: string): string {
  return stage
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function ApprovalPanel({ runId, creators }: { runId: string; creators: Creator[] }) {
  const [selected, setSelected] = useState<Set<string>>(new Set(creators.map((c) => c.id)));
  const [roster, setRoster] = useState<Creator[]>(creators);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  async function reroll(id: string) {
    setBusy(id);
    try {
      const { creator } = await api.rerollVoice(runId, id);
      setRoster((r) => r.map((c) => (c.id === id ? creator : c)));
    } catch {
      /* reroll needs the live gate + adapter; ignore if unavailable */
    } finally {
      setBusy(null);
    }
  }

  async function approve() {
    setBusy("__all__");
    try {
      await api.approve(runId, [...selected]);
      setDone(true);
    } finally {
      setBusy(null);
    }
  }

  if (done)
    return (
      <Card className="border-success-published/30 bg-success-published/5">
        <div className="flex items-center gap-2 text-success-published">
          <Icon name="check_circle" /> Roster approved — generation resumed.
        </div>
      </Card>
    );

  return (
    <Card className="border-warning-review/30">
      <SectionTitle title="Human Gate · Select Creators" />
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        {roster.map((c) => {
          const voice = c.voice_preview_uri || c.voice;
          return (
            <div
              key={c.id}
              className={`rounded-lg border p-2 flex flex-col gap-2 cursor-pointer ${
                selected.has(c.id) ? "border-primary ring-1 ring-primary" : "border-surface-border"
              }`}
              onClick={() => toggle(c.id)}
            >
              <div className="aspect-square rounded overflow-hidden bg-surface-container">
                {(c.image_uri || c.image) && (
                  <img src={mediaUrl(c.image_uri || c.image || "")} alt={c.id} className="w-full h-full object-cover" />
                )}
              </div>
              <div className="flex items-center justify-between">
                <span className="font-label-md text-label-md truncate">{c.id}</span>
                <Icon
                  name={selected.has(c.id) ? "check_circle" : "radio_button_unchecked"}
                  size={18}
                  className={selected.has(c.id) ? "text-primary" : "text-on-surface-variant"}
                />
              </div>
              {voice && voice.startsWith("/") && (
                <audio src={mediaUrl(voice)} controls className="w-full h-8" onClick={(e) => e.stopPropagation()} />
              )}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  reroll(c.id);
                }}
                disabled={busy === c.id}
                className="text-ai-processing font-label-sm text-label-sm flex items-center gap-1 hover:underline disabled:opacity-50"
              >
                <Icon name="refresh" size={14} /> {busy === c.id ? "…" : "Reroll voice"}
              </button>
            </div>
          );
        })}
      </div>
      <div className="flex justify-end mt-4">
        <Button icon="check" disabled={busy === "__all__"} onClick={approve}>
          Approve {selected.size} creator{selected.size === 1 ? "" : "s"}
        </Button>
      </div>
    </Card>
  );
}

export function CampaignDetail() {
  const { runId = "" } = useParams();
  const navigate = useNavigate();
  const run = useRunStream(runId);
  const items = Object.values(run.items);
  const pill = phasePill(run.phase);

  const stageState = (nodes: string[]): "done" | "active" | "pending" => {
    const seen = run.nodes.filter((n) => nodes.includes(n.node));
    if (seen.length && seen.every((n) => n.status === "done")) return "done";
    if (seen.length) return "active";
    return "pending";
  };

  const doneItems = items.filter((i) => i.assembled || i.dropped || i.error).length;
  const totalCost = items.reduce((a, i) => a + (i.cost_usd || 0), 0);

  return (
    <div>
      <div className="flex items-center gap-2 mb-2 font-label-md text-label-md text-on-surface-variant">
        <Link to="/campaigns" className="hover:text-primary">Campaigns</Link>
        <Icon name="chevron_right" size={16} />
        <span className="font-mono">{shortRun(runId)}</span>
      </div>

      <div className="flex items-start justify-between mb-gutter gap-4">
        <div>
          <StatusPill status={pill.status} label={pill.label} />
          <h1 className="font-headline-lg text-headline-lg text-primary mt-2">
            Campaign {shortRun(runId)}
          </h1>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" icon="visibility" onClick={() => navigate("/review")}>
            Review
          </Button>
          <Button icon="add" onClick={() => navigate("/campaigns/new")}>
            Generate More
          </Button>
        </div>
      </div>

      {/* Pipeline stages */}
      <Card className="mb-gutter">
        <SectionTitle title="Orchestration Pipeline" />
        <div className="flex items-center gap-2 overflow-x-auto">
          {STAGES.map((st, i) => {
            const state = stageState(st.nodes);
            return (
              <div key={st.key} className="flex items-center gap-2 flex-shrink-0">
                <div
                  className={`px-4 py-3 rounded-lg border min-w-[130px] ${
                    state === "done"
                      ? "border-success-published/40 bg-success-published/5"
                      : state === "active"
                      ? "border-ai-processing/40 bg-ai-processing/5"
                      : "border-surface-border bg-surface-container-low"
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <Icon
                      name={
                        state === "done" ? "check_circle" : state === "active" ? "sync" : "circle"
                      }
                      size={16}
                      className={
                        state === "done"
                          ? "text-success-published"
                          : state === "active"
                          ? "text-ai-processing animate-spin"
                          : "text-on-surface-variant"
                      }
                    />
                    <span className="font-label-md text-label-md text-primary">{st.label}</span>
                  </div>
                </div>
                {i < STAGES.length - 1 && (
                  <Icon name="arrow_forward" size={16} className="text-on-surface-variant" />
                )}
              </div>
            );
          })}
        </div>
      </Card>

      {run.phase === "awaiting" && (
        <div className="mb-gutter">
          <ApprovalPanel runId={runId} creators={run.awaiting} />
        </div>
      )}

      {run.phase === "editing" && (
        <Card className="mb-gutter border-warning-review/30 bg-warning-review/5">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-lg bg-warning-review/10 text-warning-review flex items-center justify-center flex-shrink-0">
                <Icon name="rate_review" size={22} />
              </div>
              <div>
                <h2 className="font-headline-md text-headline-md text-primary">
                  Scripts are ready for review
                </h2>
                <p className="font-body-md text-body-md text-on-surface-variant mt-1">
                  This campaign is paused at the concept edit gate.
                </p>
              </div>
            </div>
            <Button
              icon="description"
              onClick={() => navigate(`/scripts?run=${encodeURIComponent(runId)}`)}
            >
              Review Scripts
            </Button>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-12 gap-gutter">
        {/* Items */}
        <div className="col-span-12 lg:col-span-8">
          <Card>
            <SectionTitle
              title={`Clips (${doneItems}/${items.length})`}
              action={
                <span className="font-label-sm text-label-sm text-on-surface-variant">
                  {usd(totalCost)}
                </span>
              }
            />
            {items.length > 0 && (
              <ProgressBar
                value={pct(doneItems, items.length || 1)}
                tone="processing"
                className="mb-4"
              />
            )}
            {items.length === 0 && (
              <p className="font-body-md text-body-md text-on-surface-variant py-8 text-center">
                {run.phase === "idle"
                  ? "Waiting for events… (open this run while it is active)."
                  : "No clips yet — the pipeline is warming up."}
              </p>
            )}
            <div className="flex flex-col divide-y divide-surface-border">
              {items.map((it) => {
                const s = itemStatus(it);
                return (
                  <div key={it.id} className="flex items-center gap-3 py-3">
                    <div className="w-9 h-9 rounded-lg bg-surface-container flex items-center justify-center text-on-surface-variant">
                      <Icon name="movie" size={18} />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="font-label-md text-label-md text-primary truncate">
                        {it.id}
                      </div>
                      <div className="font-body-md text-body-md text-on-surface-variant truncate">
                        {it.creator_ref ? `Creator: ${it.creator_ref}` : "—"}
                      </div>
                    </div>
                    <StatusPill status={s.status} label={s.label} />
                  </div>
                );
              })}
            </div>
          </Card>
        </div>

        {/* Activity log */}
        <div className="col-span-12 lg:col-span-4">
          {Object.values(run.llmByStage).length > 0 && (
            <Card className="mb-gutter">
              <SectionTitle title="AI Stream" />
              <div className="flex flex-col gap-3 max-h-[360px] overflow-y-auto">
                {Object.values(run.llmByStage).map((stream) => (
                  <div key={stream.stage} className="rounded-lg border border-surface-border bg-surface-container-low p-3">
                    <div className="flex items-center justify-between gap-3 mb-2">
                      <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                        {stageLabel(stream.stage)}
                      </span>
                      {stream.active && <StatusPill status="processing" label="Streaming" />}
                    </div>
                    <pre className="whitespace-pre-wrap break-words font-mono text-label-sm text-on-surface-variant leading-relaxed">
                      {stream.text || "Waiting for tokens..."}
                    </pre>
                  </div>
                ))}
              </div>
            </Card>
          )}
          <Card>
            <SectionTitle title="Recent Activity" />
            <div className="flex flex-col gap-3 max-h-[420px] overflow-y-auto">
              {run.log.length === 0 && (
                <p className="font-body-md text-body-md text-on-surface-variant">No activity yet.</p>
              )}
              {[...run.log].reverse().map((l, i) => (
                <div key={i} className="flex gap-2 items-start">
                  <span className="w-1.5 h-1.5 rounded-full bg-ai-processing mt-2 flex-shrink-0" />
                  <div>
                    <div className="font-body-md text-body-md text-primary">{l.text}</div>
                    <div className="font-label-sm text-label-sm text-on-surface-variant">
                      {new Date(l.ts).toLocaleTimeString()}
                    </div>
                  </div>
                </div>
              ))}
            </div>
            {run.error && (
              <div className="mt-4 p-3 rounded-lg bg-error/5 border border-error/30 text-error font-label-md text-label-md">
                {run.error}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
