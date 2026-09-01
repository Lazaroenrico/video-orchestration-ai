import { useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { StatusPill, type Status } from "../components/StatusPill";
import { ProgressBar, StatTile } from "../components/ProgressBar";
import { RetryCampaignButton } from "../components/RetryCampaignButton";
import { PipelineProgress } from "../components/PipelineProgress";
import { useRunStream, type RunPhase } from "../api/useRunStream";
import {
  useApproveMutation,
  useRerollVoiceMutation,
  useReviewRunV2Mutation,
  useSessionQuery,
} from "../api/queries";
import { HttpError } from "../api/client";
import { mediaUrl } from "../api/urls";
import { creatorVoiceUri } from "../api/media";
import type {
  Creator,
  EditableConcept,
  GateRef,
  Item,
  ReviewCreatorPatch,
} from "../types";
import { shortRun, usd, pct } from "../lib/format";

function phasePill(phase: RunPhase): { status: Status; label: string } {
  switch (phase) {
    case "running":
      return { status: "generating", label: "Generating" };
    case "awaiting":
      return { status: "review", label: "Awaiting Approval" };
    case "review":
      return { status: "review", label: "Waiting for your review" };
    case "editing":
      return { status: "review", label: "Review Scripts" };
    case "done":
      return { status: "done", label: "Completed" };
    case "error":
      return { status: "failed", label: "Error" };
    case "cancelled":
      return { status: "failed", label: "Cancelled" };
    default:
      return { status: "draft", label: "Idle" };
  }
}

function failureStage(stage?: string | null): string {
  if (stage === "talking_head" || stage === "product_demo" || stage === "video") return "Video Generation";
  if (stage === "voiceover") return "Voiceover";
  if (stage === "assembly") return "Assembly";
  return stage ? stageLabel(stage) : "Production";
}

export function itemStatus(it: Item): { status: Status; label: string } {
  if (it.error) return { status: "failed", label: `${failureStage(it.failure?.stage)} Failed` };
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

function editableCreatorPatch(creator: Creator): ReviewCreatorPatch {
  const patch: ReviewCreatorPatch = { id: creator.id };
  for (const key of [
    "archetype",
    "visual_brief",
    "voice_brief",
    "performance_style",
    "exclusions",
    "selected_voice_candidate_id",
  ] as const) {
    const value = creator[key];
    if (value !== undefined && value !== null) {
      Object.assign(patch, { [key]: value });
    }
  }
  return patch;
}

export function CreativeReviewPanel({
  runId,
  initialConcepts,
  initialCreators,
  gate,
}: {
  runId: string;
  initialConcepts: EditableConcept[];
  initialCreators: Creator[];
  gate: GateRef | null;
}) {
  const review = useReviewRunV2Mutation();
  const session = useSessionQuery();
  const canReview = session.data ? session.data.permissions.includes("runs:review") : false;
  const [concepts, setConcepts] = useState(initialConcepts);
  const [creators, setCreators] = useState(initialCreators);
  const [staleVoiceBriefs, setStaleVoiceBriefs] = useState<Set<string>>(
    new Set(),
  );
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submitLock = useRef(false);
  const [submission, setSubmission] = useState<
    "idle" | "submitting" | "accepted" | "refreshing"
  >("idle");
  const [activeAction, setActiveAction] = useState<string | null>(null);

  if (!canReview) {
    return (
      <Card className="mb-gutter border-surface-border">
        <SectionTitle title="Revisão do Plano Criativo" />
        <p className="mt-2 font-body-md text-body-md text-on-surface-variant">
          A revisão e aprovação de planos criativos é restrita a membros com permissão de edição.
        </p>
      </Card>
    );
  }

  function editConcept(index: number, key: string, value: string) {
    setConcepts((current) =>
      current.map((concept, position) =>
        position === index ? { ...concept, [key]: value } : concept,
      ),
    );
  }

  function editCreator(index: number, key: string, value: string) {
    setCreators((current) =>
      current.map((creator, position) =>
        position === index
          ? {
              ...creator,
              [key]: value,
              ...(key === "voice_brief"
                ? { selected_voice_candidate_id: null }
                : {}),
            }
          : creator,
      ),
    );
    if (key === "voice_brief") {
      setStaleVoiceBriefs((current) => new Set(current).add(creators[index].id));
    }
  }

  function selectVoice(index: number, candidateId: string) {
    setCreators((current) =>
      current.map((creator, position) =>
        position === index
          ? { ...creator, selected_voice_candidate_id: candidateId }
          : creator,
      ),
    );
  }

  async function submit(
    action: "approve" | "regenerate",
    target?: "concepts" | "scripts" | "creators" | "voices",
  ) {
    if (submitLock.current) return;
    submitLock.current = true;
    setSubmission("submitting");
    setActiveAction(action === "approve" ? action : `${action}:${target}`);
    setError(null);
    try {
      const creatorPatches = creators.map(editableCreatorPatch);
      const ids =
        target === "concepts" || target === "scripts"
          ? concepts.map((concept) => String(concept.id))
          : creators.map((creator) => creator.id);
      await review.mutateAsync({
        runId,
        action,
        gate,
        ...(action === "approve"
          ? { concepts, creators: creatorPatches }
          : {
              target,
              ids,
              feedback: feedback.trim(),
              ...(target === "creators" || target === "voices"
                ? { creators: creatorPatches }
                : { concepts }),
            }),
      });
      setSubmission("accepted");
    } catch (caught) {
      if (caught instanceof HttpError && caught.status === 409) {
        setSubmission("refreshing");
        return;
      }
      submitLock.current = false;
      setSubmission("idle");
      setActiveAction(null);
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  if (submission === "accepted" || submission === "refreshing") {
    const accepted = submission === "accepted";
    return (
      <Card className="border-warning-review/30">
        <div
          role="status"
          className="flex min-h-32 items-center gap-3 text-primary"
        >
          <Icon name={accepted ? "check_circle" : "sync"} size={24} />
          <div>
            <h2 className="font-headline-md text-headline-md">
              {accepted
                ? "Revisão enviada. Iniciando a próxima etapa..."
                : "Esta revisão já foi processada. Atualizando a campanha..."}
            </h2>
          </div>
        </div>
      </Card>
    );
  }

  const controlsLocked = submission === "submitting";
  const allVoicesSelected = creators.every((creator) => {
    if (staleVoiceBriefs.has(creator.id)) return false;
    const candidateIds = new Set(
      (creator.voice_candidates || []).map((candidate) => candidate.candidate_id),
    );
    return Boolean(
      creator.selected_voice_candidate_id &&
        candidateIds.has(creator.selected_voice_candidate_id),
    );
  });

  return (
    <Card className="border-warning-review/30">
      <SectionTitle title="Revisar plano criativo" />
      <div className="mb-6">
        <h3 className="mb-3 font-headline-md text-headline-md text-primary">
          Creators
        </h3>
        <div className="grid gap-3 sm:grid-cols-2">
          {creators.map((creator, index) => {
            const candidates = creator.voice_candidates || [];
            const voiceBriefIsStale = staleVoiceBriefs.has(creator.id);
            return (
              <div key={creator.id} className="grid gap-4 border-b border-surface-border pb-5 sm:grid-cols-[112px_1fr]">
                <div className="aspect-square overflow-hidden rounded-lg bg-surface-container">
                  {(creator.image_uri || creator.image) && (
                    <img
                      src={mediaUrl(creator.image_uri || creator.image || "")}
                      alt={creator.archetype || creator.id}
                      className="size-full object-cover"
                    />
                  )}
                </div>
                <div className="min-w-0 space-y-2">
                  <input
                    className="hm-field"
                    value={creator.archetype || creator.id}
                    onChange={(event) => editCreator(index, "archetype", event.target.value)}
                    aria-label={`Arquétipo do creator ${index + 1}`}
                  />
                  <textarea
                    className="hm-field"
                    rows={2}
                    value={creator.performance_style || ""}
                    onChange={(event) =>
                      editCreator(index, "performance_style", event.target.value)
                    }
                    placeholder="Estilo de performance"
                    aria-label={`Performance do creator ${index + 1}`}
                  />
                  <textarea
                    className="hm-field"
                    rows={2}
                    value={creator.voice_brief || ""}
                    onChange={(event) =>
                      editCreator(index, "voice_brief", event.target.value)
                    }
                    placeholder="Direção vocal"
                    aria-label={`Brief de voz do creator ${index + 1}`}
                  />
                  <fieldset className="space-y-2" aria-label={`Vozes do creator ${index + 1}`}>
                    <legend className="font-label-sm text-label-sm uppercase tracking-[0.12em] text-on-surface-variant">
                      Escolha uma voz
                    </legend>
                    {voiceBriefIsStale && (
                      <p className="font-label-sm text-label-sm text-warning-review">
                        Brief alterado — regenere as vozes antes de selecionar.
                      </p>
                    )}
                    {candidates.map((candidate, candidateIndex) => {
                      const selected =
                        creator.selected_voice_candidate_id === candidate.candidate_id;
                      return (
                        <label
                          key={candidate.candidate_id}
                          className={`grid cursor-pointer gap-2 rounded-lg border p-3 transition-colors ${
                            selected
                              ? "border-primary bg-primary/5"
                              : "border-surface-border bg-surface-container-low hover:border-primary/50"
                          } ${voiceBriefIsStale ? "cursor-not-allowed opacity-55" : ""}`}
                        >
                          <span className="flex items-center gap-2 font-label-sm text-label-sm text-primary">
                            <input
                              type="radio"
                              name={`voice-${creator.id}`}
                              value={candidate.candidate_id}
                              checked={selected}
                              disabled={voiceBriefIsStale || controlsLocked}
                              onChange={() => selectVoice(index, candidate.candidate_id)}
                            />
                            Opção {String(candidateIndex + 1).padStart(2, "0")}
                            <span className="ml-auto text-on-surface-variant">
                              {candidate.duration_seconds.toFixed(1)}s
                            </span>
                          </span>
                          <audio
                            src={mediaUrl(candidate.preview.uri)}
                            controls
                            preload="metadata"
                            className="h-8 w-full"
                            onClick={(event) => event.stopPropagation()}
                          />
                        </label>
                      );
                    })}
                  </fieldset>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div>
        <h3 className="mb-3 font-headline-md text-headline-md text-primary">
          Pacotes criativos
        </h3>
        <div className="divide-y divide-surface-border">
          {concepts.map((concept, index) => (
            <div key={String(concept.id || index)} className="grid gap-3 py-4">
              <div className="font-label-sm text-label-sm text-on-surface-variant">
                Pacote {index + 1}
              </div>
              <input
                className="hm-field"
                value={String(concept.hook || "")}
                onChange={(event) => editConcept(index, "hook", event.target.value)}
                aria-label={`Hook do pacote ${index + 1}`}
              />
              <textarea
                className="hm-field"
                rows={2}
                value={String(concept.angle || "")}
                onChange={(event) => editConcept(index, "angle", event.target.value)}
                aria-label={`Ângulo do pacote ${index + 1}`}
              />
              <textarea
                className="hm-field font-mono text-label-sm"
                rows={7}
                value={String(concept.script || "")}
                onChange={(event) => editConcept(index, "script", event.target.value)}
                aria-label={`Roteiro do pacote ${index + 1}`}
              />
              <div className="font-label-sm text-label-sm text-on-surface-variant">
                Evidência: {String(concept.evidence_basis || "cold_test")}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="mt-5 border-t border-surface-border pt-4">
        <label
          htmlFor="review-feedback"
          className="mb-1 block font-label-sm text-label-sm uppercase text-on-surface-variant"
        >
          Ajuste para regeneração
        </label>
        <textarea
          id="review-feedback"
          className="hm-field"
          rows={3}
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
          placeholder="Descreva objetivamente o que precisa mudar."
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            variant="secondary"
            icon="refresh"
            disabled={controlsLocked}
            loading={activeAction === "regenerate:concepts"}
            onClick={() => submit("regenerate", "concepts")}
          >
            Regenerar conceitos
          </Button>
          <Button
            variant="secondary"
            icon="refresh"
            disabled={controlsLocked}
            loading={activeAction === "regenerate:scripts"}
            onClick={() => submit("regenerate", "scripts")}
          >
            Regenerar roteiros
          </Button>
          <Button
            variant="secondary"
            icon="refresh"
            disabled={controlsLocked}
            loading={activeAction === "regenerate:creators"}
            onClick={() => submit("regenerate", "creators")}
          >
            Regenerar creators
          </Button>
          <Button
            variant="secondary"
            icon="refresh"
            disabled={controlsLocked}
            loading={activeAction === "regenerate:voices"}
            onClick={() => submit("regenerate", "voices")}
          >
            Regenerar vozes
          </Button>
          <Button
            icon="check"
            disabled={controlsLocked || !allVoicesSelected}
            loading={activeAction === "approve"}
            onClick={() => submit("approve")}
          >
            Aprovar e produzir
          </Button>
        </div>
        {error && (
          <p role="alert" className="mt-3 font-body-md text-body-md text-error">
            {error}
          </p>
        )}
      </div>
    </Card>
  );
}

function ApprovalPanel({
  runId,
  creators,
  gate,
}: {
  runId: string;
  creators: Creator[];
  gate: GateRef | null;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set(creators.map((c) => c.id)));
  const [roster, setRoster] = useState<Creator[]>(creators);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const approveCreators = useApproveMutation();
  const rerollVoice = useRerollVoiceMutation();
  const session = useSessionQuery();
  const canReview = session.data ? session.data.permissions.includes("runs:review") : false;

  if (!canReview) return null;

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  async function reroll(id: string) {
    setBusy(id);
    setActionError(null);
    try {
      const { creator } = await rerollVoice.mutateAsync({ runId, creatorId: id });
      setRoster((r) => r.map((c) => (c.id === id ? creator : c)));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not reroll this voice.");
    } finally {
      setBusy(null);
    }
  }

  async function approve() {
    setBusy("__all__");
    setActionError(null);
    try {
      await approveCreators.mutateAsync({ runId, approved: [...selected], gate });
      setDone(true);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not approve the selected creators.");
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
          const voice = creatorVoiceUri(c);
          return (
            <div
              key={c.id}
              className={`rounded-lg border p-2 flex flex-col gap-2 cursor-pointer focus-within:ring-2 focus-within:ring-primary ${
                selected.has(c.id) ? "border-primary ring-1 ring-primary" : "border-surface-border"
              }`}
              onClick={() => toggle(c.id)}
              role="checkbox"
              aria-checked={selected.has(c.id)}
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === " " || event.key === "Enter") {
                  event.preventDefault();
                  toggle(c.id);
                }
              }}
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
              {voice && (
                <audio src={mediaUrl(voice)} controls className="w-full h-8" onClick={(e) => e.stopPropagation()} />
              )}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  reroll(c.id);
                }}
                disabled={busy === c.id}
                className="inline-flex min-h-11 items-center gap-1 whitespace-nowrap text-ai-processing font-label-sm text-label-sm hover:underline disabled:opacity-50"
              >
                <Icon name="refresh" size={14} /> {busy === c.id ? "…" : "Reroll voice"}
              </button>
            </div>
          );
        })}
      </div>
      {actionError && (
        <p role="alert" className="mt-3 rounded-lg border border-error/30 bg-error/5 px-3 py-2 font-body-md text-body-md text-error">
          {actionError}
        </p>
      )}
      <div className="flex justify-end mt-4">
        <Button icon="check" loading={busy === "__all__"} onClick={approve}>
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
  const [itemView, setItemView] = useState<"all" | "attention" | "finished">("all");

  const doneItems = items.filter((i) => i.assembled || i.dropped || i.error).length;
  const totalCost = items.reduce((a, i) => a + (i.cost_usd || 0), 0);
  const attentionItems = items.filter((i) => i.dropped || i.error || (i.qc && !i.qc.passed));
  const visibleItems =
    itemView === "attention"
      ? attentionItems
      : itemView === "finished"
      ? items.filter((i) => i.assembled || i.dropped || i.error)
      : items;

  return (
    <div>
      <div className="flex items-center gap-2 mb-2 font-label-md text-label-md text-on-surface-variant">
        <Link to="/campaigns" className="hover:text-primary">Campaigns</Link>
        <Icon name="chevron_right" size={16} />
        <span className="font-mono">{shortRun(runId)}</span>
      </div>

      <div className="mb-gutter flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <StatusPill status={pill.status} label={pill.label} />
          <h1 className="hm-page-title mt-2 text-primary">
            Campaign {shortRun(runId)}
          </h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" icon="visibility" onClick={() => navigate(`/review?run=${encodeURIComponent(runId)}`)}>
            Review
          </Button>
          <Button icon="add" onClick={() => navigate("/campaigns/new")}>
            Generate More
          </Button>
        </div>
      </div>

      <div className="mb-gutter grid grid-cols-2 gap-3 xl:grid-cols-4">
        <StatTile label="Finished clips" value={`${doneItems}/${items.length || "—"}`} />
        <StatTile label="Needs attention" value={attentionItems.length} hint={attentionItems.length ? "QC / production" : "Clear"} hintTone={attentionItems.length ? "error" : "success"} />
        <StatTile label="In progress" value={Math.max(0, items.length - doneItems)} hint={run.phase === "running" ? "Live" : undefined} hintTone="muted" />
        <StatTile label="Run cost" value={usd(totalCost)} />
      </div>

      {run.phase === "error" && (
        <Card className="mb-gutter border-error/40 bg-error/5">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex min-w-0 gap-3">
              <Icon name="error" className="mt-0.5 shrink-0 text-error" />
              <div>
                <h2 className="font-headline-md text-headline-md text-primary">This run stopped with an error</h2>
                <p className="mt-1 break-words font-body-md text-body-md text-on-surface-variant">
                  {run.error || "The runtime did not provide an error message."}
                </p>
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <RetryCampaignButton runId={runId} />
              <Button variant="secondary" icon="visibility" onClick={() => navigate(`/review?run=${encodeURIComponent(runId)}`)}>
                Review available clips
              </Button>
            </div>
          </div>
        </Card>
      )}

      {run.phase === "awaiting" && (
        <div className="mb-gutter">
          <ApprovalPanel runId={runId} creators={run.awaiting} gate={run.gate} />
        </div>
      )}

      {run.phase === "review" && run.review && (
        <div className="mb-gutter">
          <CreativeReviewPanel
            key={`${run.gate?.gate_id ?? "local"}:${run.gate?.version ?? 0}`}
            runId={runId}
            initialConcepts={run.review.concepts}
            initialCreators={run.review.creators}
            gate={run.gate}
          />
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
                <div className="flex flex-wrap gap-1" aria-label="Clip filter">
                  {([
                    ["all", "All"],
                    ["attention", `Attention (${attentionItems.length})`],
                    ["finished", "Finished"],
                  ] as const).map(([view, label]) => (
                    <button
                      key={view}
                      type="button"
                      onClick={() => setItemView(view)}
                      className={`min-h-9 whitespace-nowrap rounded-md px-2 font-label-sm text-label-sm ${itemView === view ? "bg-primary text-on-primary" : "text-on-surface-variant hover:bg-surface-container-low"}`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
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
              {visibleItems.map((it) => {
                const s = itemStatus(it);
                const itemProgress = run.progress?.items.find((entry) => entry.item_id === it.id);
                const progressStage = run.progress?.stages.find((stage) => stage.id === itemProgress?.stage_id);
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
                        {progressStage
                          ? `${progressStage.label}${itemProgress?.attempt ? ` · attempt ${itemProgress.attempt}` : ""}`
                          : it.creator_ref
                            ? `Creator: ${it.creator_ref}`
                            : "—"}
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
          <Card className="mb-gutter">
            <SectionTitle title="Pipeline progress" />
            {run.progress ? (
              <PipelineProgress progress={run.progress} />
            ) : (
              <p className="font-body-md text-body-md text-on-surface-variant">
                Waiting for the first progress snapshot.
              </p>
            )}
          </Card>
          <Card className="mb-gutter">
            <SectionTitle title="Recent activity" />
            <div className="flex max-h-[420px] flex-col gap-3 overflow-y-auto" aria-live="polite">
              {run.activity.length === 0 && (
                <p className="font-body-md text-body-md text-on-surface-variant">No activity yet.</p>
              )}
              {[...run.activity].reverse().map((entry) => (
                <div key={entry.event_id} className="flex items-start gap-3">
                  <span
                    className={`mt-0.5 flex size-7 shrink-0 items-center justify-center ${
                      entry.status === "failed"
                        ? "text-error"
                        : entry.status === "completed"
                          ? "text-success-published"
                          : entry.status === "waiting"
                            ? "text-warning-review"
                            : "text-ai-processing"
                    }`}
                  >
                    <Icon
                      name={
                        entry.status === "failed"
                          ? "error"
                          : entry.status === "completed"
                            ? "check_circle"
                            : entry.status === "waiting"
                              ? "pause_circle"
                              : "arrow_right_alt"
                      }
                      size={17}
                    />
                  </span>
                  <div className="min-w-0">
                    <div className="font-body-md text-body-md text-primary">{entry.label}</div>
                    {(entry.item_id || entry.attempt) && (
                      <div className="truncate font-mono text-label-sm text-on-surface-variant">
                        {[entry.item_id, entry.attempt ? `attempt ${entry.attempt}` : null].filter(Boolean).join(" · ")}
                      </div>
                    )}
                    {entry.occurred_at && (
                      <div className="font-label-sm text-label-sm text-on-surface-variant">
                        {new Date(entry.occurred_at).toLocaleTimeString()}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
            {run.error && (
              <div className="mt-4 rounded-lg border border-error/30 bg-error/5 p-3 font-label-md text-label-md text-error">
                {run.error}
              </div>
            )}
          </Card>
          {Object.values(run.llmByStage).length > 0 && (
            <Card>
              <SectionTitle title="Live model output" />
              <div className="flex flex-col gap-3 max-h-[360px] overflow-y-auto" aria-live="polite">
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
        </div>
      </div>
    </div>
  );
}
