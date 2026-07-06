import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { StatusPill } from "../components/StatusPill";
import { RunSelect } from "../components/RunSelect";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useRunSelection } from "../api/useRunSelection";
import { useRunStream } from "../api/useRunStream";
import { api } from "../api/client";
import type { EditableConcept, Item } from "../types";

function conceptField(it: Item, key: string): string | null {
  const c = it.concept as Record<string, unknown> | null | undefined;
  const v = c?.[key];
  return typeof v === "string" ? v : v != null ? JSON.stringify(v) : null;
}

function conceptTitle(it: Item): string {
  return conceptField(it, "hook") || conceptField(it, "title") || conceptField(it, "angle") || it.id;
}

interface ConceptDraft {
  concept: EditableConcept;
  included: boolean;
}

function editableText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  return JSON.stringify(value);
}

function draftTitle(draft: ConceptDraft): string {
  const c = draft.concept;
  for (const key of ["hook", "title", "angle", "id"]) {
    const value = c[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "Untitled concept";
}

export function Concepts() {
  const [searchParams] = useSearchParams();
  const preferredRunId = searchParams.get("run");
  const { runs, active, selected, setSelected, loading, error } =
    useRunSelection(preferredRunId);
  const run = useRunStream(selected);
  const items = Object.values(run.items).filter((i) => i.concept || i.script);
  const [pick, setPick] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<ConceptDraft[]>([]);
  const [saving, setSaving] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const editKey = useMemo(
    () => run.editConcepts.map((c) => String(c.id)).join("|"),
    [run.editConcepts]
  );

  useEffect(() => {
    if (!pick && items.length) setPick(items[0].id);
  }, [items, pick]);

  useEffect(() => {
    if (run.phase !== "editing") return;
    const next = run.editConcepts.map((concept) => ({
      concept: { ...concept },
      included: true,
    }));
    setDrafts(next);
    setPick((current) => current ?? String(next[0]?.concept.id ?? ""));
    setSubmitError(null);
  }, [run.phase, editKey]);

  const updateDraft = (id: string, key: string, value: string) => {
    setDrafts((current) =>
      current.map((draft) =>
        String(draft.concept.id) === id
          ? { ...draft, concept: { ...draft.concept, [key]: value } }
          : draft
      )
    );
  };

  const toggleDraft = (id: string) => {
    setDrafts((current) =>
      current.map((draft) =>
        String(draft.concept.id) === id
          ? { ...draft, included: !draft.included }
          : draft
      )
    );
  };

  const submitDrafts = async () => {
    if (!selected) return;
    setSaving(true);
    setSubmitError(null);
    try {
      await api.submitConcepts(
        selected,
        drafts.filter((draft) => draft.included).map((draft) => draft.concept)
      );
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Could not save concepts");
    } finally {
      setSaving(false);
    }
  };

  const current = items.find((i) => i.id === pick) ?? items[0] ?? null;
  const currentDraft =
    drafts.find((draft) => String(draft.concept.id) === pick) ?? drafts[0] ?? null;

  return (
    <div>
      <PageHeader
        title="Concepts & Scripts Review"
        subtitle="Review generated concepts and scripts as they stream in."
        actions={<RunSelect runs={runs} active={active} selected={selected} onChange={setSelected} />}
      />

      {loading && <Loading />}
      {error && <ErrorState message={error} />}
      {!loading && !error && runs.length === 0 && (
        <EmptyState icon="description" title="No scripts yet" hint="Concepts and scripts appear here as a run produces them." />
      )}

      {!loading && !error && runs.length > 0 && (
        run.phase === "editing" ? (
          <div className="grid grid-cols-12 gap-gutter">
            <div className="col-span-12 lg:col-span-4 flex flex-col gap-3">
              {drafts.map((draft) => {
                const id = String(draft.concept.id);
                return (
                  <button
                    key={id}
                    onClick={() => setPick(id)}
                    className={`text-left rounded-xl border p-4 bg-surface-container-lowest transition-colors ${
                      pick === id ? "border-primary ring-1 ring-primary" : "border-surface-border hover:bg-surface-container-low"
                    } ${!draft.included ? "opacity-60" : ""}`}
                  >
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span className="font-headline-md text-headline-md text-primary truncate">
                        {draftTitle(draft)}
                      </span>
                      <StatusPill
                        status={draft.included ? "approved" : "failed"}
                        label={draft.included ? "Included" : "Excluded"}
                      />
                    </div>
                    <p className="font-body-md text-body-md text-on-surface-variant line-clamp-2">
                      {editableText(draft.concept.script || draft.concept.angle || "No script")}
                    </p>
                  </button>
                );
              })}
            </div>

            <div className="col-span-12 lg:col-span-8">
              {currentDraft ? (
                <Card>
                  <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4 mb-5">
                    <div>
                      <h2 className="font-headline-lg text-headline-lg text-primary">
                        {draftTitle(currentDraft)}
                      </h2>
                      <p className="font-label-md text-label-md text-on-surface-variant mt-1">
                        {String(currentDraft.concept.id)}
                      </p>
                    </div>
                    <label className="inline-flex items-center gap-2 font-label-md text-label-md text-primary">
                      <input
                        type="checkbox"
                        checked={currentDraft.included}
                        onChange={() => toggleDraft(String(currentDraft.concept.id))}
                        className="h-4 w-4 rounded border-surface-border text-primary focus:ring-primary"
                      />
                      Include
                    </label>
                  </div>

                  <div className="grid sm:grid-cols-2 gap-4 mb-5">
                    {Object.entries(currentDraft.concept)
                      .filter(([key]) => key !== "script")
                      .map(([key, value]) => (
                        <label key={key} className="block">
                          <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                            {key}
                          </span>
                          <textarea
                            value={editableText(value)}
                            onChange={(ev) =>
                              updateDraft(String(currentDraft.concept.id), key, ev.target.value)
                            }
                            rows={key === "id" ? 1 : 3}
                            readOnly={key === "id"}
                            className="mt-2 w-full resize-y rounded-lg border border-surface-border bg-surface-container-low px-3 py-2 font-body-md text-body-md text-primary outline-none focus:border-primary focus:ring-1 focus:ring-primary disabled:opacity-60"
                          />
                        </label>
                      ))}
                  </div>

                  <label className="block">
                    <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant flex items-center gap-2">
                      <Icon name="description" size={16} /> Script
                    </span>
                    <textarea
                      value={editableText(currentDraft.concept.script)}
                      onChange={(ev) =>
                        updateDraft(String(currentDraft.concept.id), "script", ev.target.value)
                      }
                      rows={12}
                      className="mt-2 w-full resize-y rounded-lg border border-surface-border bg-surface-container-low px-4 py-3 font-body-md text-body-md text-primary leading-relaxed outline-none focus:border-primary focus:ring-1 focus:ring-primary"
                    />
                  </label>

                  {submitError && (
                    <div className="mt-4 rounded-lg border border-error/40 bg-error/10 px-4 py-3 font-body-md text-body-md text-error">
                      {submitError}
                    </div>
                  )}

                  <div className="mt-5 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                    <p className="font-body-md text-body-md text-on-surface-variant">
                      {drafts.filter((draft) => draft.included).length} of {drafts.length} concepts selected
                    </p>
                    <button
                      type="button"
                      disabled={saving}
                      onClick={submitDrafts}
                      className="inline-flex items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2 font-label-md text-label-md text-on-primary transition-opacity disabled:opacity-60"
                    >
                      <Icon name="check" size={16} />
                      {saving ? "Saving…" : "Save & Continue"}
                    </button>
                  </div>
                </Card>
              ) : (
                <Card className="text-on-surface-variant">Waiting for editable concepts…</Card>
              )}
            </div>
          </div>
        ) : (
        <div className="grid grid-cols-12 gap-gutter">
          {/* List */}
          <div className="col-span-12 lg:col-span-4 flex flex-col gap-3">
            {items.length === 0 && (
              <Card className="text-on-surface-variant font-body-md text-body-md">
                Waiting for concepts…
              </Card>
            )}
            {items.map((it) => (
              <button
                key={it.id}
                onClick={() => setPick(it.id)}
                className={`text-left rounded-xl border p-4 bg-surface-container-lowest transition-colors ${
                  pick === it.id ? "border-primary ring-1 ring-primary" : "border-surface-border hover:bg-surface-container-low"
                }`}
              >
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="font-headline-md text-headline-md text-primary truncate">
                    {conceptTitle(it)}
                  </span>
                  <StatusPill
                    status={it.dropped ? "failed" : it.script ? "approved" : "draft"}
                    label={it.dropped ? "Dropped" : it.script ? "Scripted" : "Concept"}
                  />
                </div>
                <p className="font-body-md text-body-md text-on-surface-variant line-clamp-2">
                  {it.script ? it.script.slice(0, 120) : conceptField(it, "angle") || "—"}
                </p>
              </button>
            ))}
          </div>

          {/* Detail */}
          <div className="col-span-12 lg:col-span-8">
            {current ? (
              <Card>
                <div className="flex items-start justify-between mb-4">
                  <div>
                    <h2 className="font-headline-lg text-headline-lg text-primary">
                      {conceptTitle(current)}
                    </h2>
                    <p className="font-label-md text-label-md text-on-surface-variant mt-1">
                      {current.id} · tier {current.tier ?? "—"}
                    </p>
                  </div>
                  <StatusPill
                    status={current.dropped ? "failed" : current.script ? "approved" : "draft"}
                    label={current.dropped ? "Dropped" : current.script ? "Scripted" : "Concept"}
                  />
                </div>

                {current.concept && (
                  <div className="mb-6">
                    <div className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant mb-2">
                      Concept
                    </div>
                    <div className="grid sm:grid-cols-2 gap-2">
                      {Object.entries(current.concept as Record<string, unknown>).map(([k, v]) => (
                        <div key={k} className="p-3 rounded-lg bg-surface-container-low">
                          <div className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                            {k}
                          </div>
                          <div className="font-body-md text-body-md text-primary break-words">
                            {typeof v === "string" ? v : JSON.stringify(v)}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div>
                  <div className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant mb-2 flex items-center gap-2">
                    <Icon name="description" size={16} /> Script
                  </div>
                  <div className="p-4 rounded-lg bg-surface-container-low font-body-md text-body-md text-primary whitespace-pre-wrap leading-relaxed">
                    {current.script || "Script not generated yet."}
                  </div>
                </div>
              </Card>
            ) : (
              <Card className="text-on-surface-variant">Select a concept to review.</Card>
            )}
          </div>
        </div>
        )
      )}
    </div>
  );
}
