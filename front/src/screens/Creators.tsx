import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
import { Icon } from "../components/Icon";
import { Drawer } from "../components/Drawer";
import { Loading, ErrorState, EmptyState } from "../components/States";
import { errorMessage, useCreators, useSessionQuery, useStartRunMutation } from "../api/queries";
import { mediaUrl } from "../api/urls";
import { creatorVoiceUri } from "../api/media";
import type { Creator } from "../types";

function CreatorCard({ c, onOpen }: { c: Creator; onOpen: () => void }) {
  const img = c.image_uri || c.image || "";
  const voice = creatorVoiceUri(c);
  return (
    <Card padded={false} className="overflow-hidden flex flex-col">
      <div className="aspect-[4/5] bg-surface-container overflow-hidden">
        {img ? (
          <img src={mediaUrl(img)} alt={c.id} className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-on-surface-variant">
            <Icon name="person" size={48} />
          </div>
        )}
      </div>
      <div className="p-4 flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="font-headline-md text-headline-md text-primary truncate">{c.id}</span>
          <span className="inline-flex items-center gap-1 font-label-sm text-label-sm text-success-published">
            <Icon name="check_circle" size={14} /> Media ready
          </span>
        </div>
        <p className="font-body-md text-body-md text-on-surface-variant truncate">
          {c.offer ? `Offer: ${c.offer}` : "Synthetic UGC creator"}
        </p>
        <div className="flex flex-wrap gap-1">
          {(c.angles.length ? c.angles : ["Talking Head"]).slice(0, 3).map((a) => (
            <span
              key={a}
              className="px-2 py-0.5 rounded-full bg-surface-container-high font-label-sm text-label-sm text-on-surface-variant"
            >
              {a}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2 mt-1">
          <Button variant="secondary" icon="visibility" className="flex-1" onClick={onOpen}>
            Details
          </Button>
          {voice && (
            <a
              href={mediaUrl(voice)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center justify-center w-10 h-10 rounded-lg border border-surface-border text-ai-processing hover:bg-surface-container-low"
              title="Voice preview"
            >
              <Icon name="graphic_eq" />
            </a>
          )}
        </div>
      </div>
    </Card>
  );
}

export function Creators() {
  const creatorsQuery = useCreators();
  const data = creatorsQuery.data ?? null;
  const loading = creatorsQuery.isLoading;
  const error = errorMessage(creatorsQuery.error);
  const startRun = useStartRunMutation();
  const session = useSessionQuery();
  const canCreate = session.data ? session.data.permissions.includes("runs:create") : false;
  const navigate = useNavigate();
  const [selected, setSelected] = useState<Creator | null>(null);
  const [query, setQuery] = useState("");
  const [draftOffer, setDraftOffer] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);

  const creators = data?.creators ?? [];
  const filtered = useMemo(
    () => creators.filter((c) => c.id.toLowerCase().includes(query.toLowerCase())),
    [creators, query]
  );

  useEffect(() => {
    setDraftOffer(selected?.offer ?? "");
    setDraftError(null);
  }, [selected?.id, selected?.offer, selected?.run_id]);

  const launchDraft = async () => {
    if (!selected) return;
    setDraftError(null);
    try {
      const { run_id } = await startRun.mutateAsync({
        offer: draftOffer.trim() || selected.offer || "creator draft",
        batch: 1,
        platform: "tiktok",
        creator_id: selected.id,
        creator_run_id: selected.run_id ?? null,
        approve_creators: false,
        edit_concepts: true,
      });
      navigate(`/scripts?run=${encodeURIComponent(run_id)}`);
    } catch (err) {
      setDraftError(err instanceof Error ? err.message : "Could not start draft run");
    }
  };

  return (
    <div>
      <PageHeader
        title="AI Creators Library"
        subtitle="Reuse generated creators for draft videos and review their available media."
        actions={<Button icon="add" onClick={() => navigate("/campaigns/new")}>Run Campaign</Button>}
      />

      <div className="grid grid-cols-12 gap-gutter">
        {/* Filters rail */}
        <aside className="col-span-12 lg:col-span-3">
          <Card>
            <div className="flex items-center gap-2 mb-4 text-primary">
              <Icon name="filter_list" />
              <span className="font-headline-md text-headline-md">Filters</span>
            </div>
            <label className="block font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant mb-1">
              Search
            </label>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="creator id…"
              className="hm-field"
            />
            <div className="mt-4 font-body-md text-body-md text-on-surface-variant">
              {creators.length} creator{creators.length === 1 ? "" : "s"} with complete media
              (image + voice).
            </div>
            <p className="mt-2 font-label-sm text-label-sm text-on-surface-variant">
              Source: <span className="font-mono">{data?.store_path ?? "—"}</span>
            </p>
          </Card>
        </aside>

        {/* Grid */}
        <section className="col-span-12 lg:col-span-9">
          {loading && <Loading label="Loading creators…" />}
          {error && <ErrorState message={error} />}
          {!loading && !error && filtered.length === 0 && (
            <EmptyState
              icon="groups"
              title="No creators yet"
              hint="Creators appear here once a run produces a person with both a rendered image and a playable voice. Start a campaign to generate some."
            />
          )}
          {!loading && !error && filtered.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-gutter">
              {filtered.map((c) => (
                <CreatorCard key={`${c.run_id ?? ""}-${c.id}`} c={c} onOpen={() => setSelected(c)} />
              ))}
            </div>
          )}
        </section>
      </div>

      <Drawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={selected?.id ?? ""}
        footer={
          selected && canCreate ? (
            <Button icon="movie" className="w-full" onClick={launchDraft} loading={startRun.isPending}>
              {startRun.isPending ? "Starting draft" : `Draft Video with ${selected.id}`}
            </Button>
          ) : null
        }
      >
        {selected && (
          <div className="flex flex-col gap-6">
            <div className="aspect-[4/5] rounded-xl overflow-hidden bg-surface-container">
              {(selected.image_uri || selected.image) && (
                <img
                  src={mediaUrl(selected.image_uri || selected.image || "")}
                  alt={selected.id}
                  className="w-full h-full object-cover"
                />
              )}
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="flex flex-col">
                <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                  Media
                </span>
                <span className="font-headline-md text-headline-md text-primary flex items-center gap-1">
                  <Icon name="check_circle" size={18} className="text-success-published" /> Ready
                </span>
              </div>
              <div className="flex flex-col">
                <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                  Angles
                </span>
                <span className="font-headline-md text-headline-md text-primary">
                  {selected.angles.length}
                </span>
              </div>
            </div>
            {creatorVoiceUri(selected) && (
              <div>
                <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                  Voice preview
                </span>
                <audio src={mediaUrl(creatorVoiceUri(selected)!)} controls className="w-full mt-2" />
              </div>
            )}
            <label className="block">
              <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                Product / Offer
              </span>
              <input
                value={draftOffer}
                onChange={(e) => {
                  setDraftOffer(e.target.value);
                  if (draftError) setDraftError(null);
                }}
                placeholder={selected.offer || "creator draft"}
                className="hm-field mt-2"
              />
            </label>
            {draftError && (
              <div className="rounded-lg border border-error/30 bg-error/5 px-3 py-2 font-body-md text-body-md text-error">
                {draftError}
              </div>
            )}
            <div>
              <span className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
                Source run
              </span>
              <p className="font-mono text-body-md text-on-surface-variant break-all">
                {selected.run_id ?? "—"}
              </p>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
