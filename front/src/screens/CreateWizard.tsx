import { useState } from "react";
import { useNavigate } from "react-router";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { api } from "../api/client";

const PLATFORMS = ["tiktok", "instagram", "youtube"] as const;
const STEPS = ["Offer", "Direction", "Quality Gates", "Review"];

const label =
  "block font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant mb-1";
const field = "hm-field";

export function CreateWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [offer, setOffer] = useState("");
  const [creatorPrompt, setCreatorPrompt] = useState("");
  const [videoPrompt, setVideoPrompt] = useState("");
  const [batch, setBatch] = useState(6);
  const [platform, setPlatform] = useState<(typeof PLATFORMS)[number]>("tiktok");
  const [editConcepts, setEditConcepts] = useState(true);
  const [approveCreators, setApproveCreators] = useState(true);

  const canContinue = step === 0 ? offer.trim().length > 0 : true;

  async function launch() {
    setSubmitting(true);
    setError(null);
    try {
      const { run_id } = await api.startRun({
        offer: offer.trim(),
        batch,
        platform,
        creator_prompt: creatorPrompt.trim() || null,
        video_prompt: videoPrompt.trim() || null,
        edit_concepts: editConcepts,
        approve_creators: approveCreators,
      });
      navigate(`/campaigns/${run_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-[100dvh] bg-background px-4 py-6 sm:px-6 lg:px-margin-desktop lg:py-10">
      <div className="mx-auto max-w-3xl">
        <div className="mb-gutter flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="hm-page-title mb-1 text-primary">
              Create New Campaign
            </h1>
            <p className="font-body-md text-body-md text-on-surface-variant">
              Set the production brief, then choose where people review the work.
            </p>
          </div>
          <button
            type="button"
            onClick={() => navigate("/")}
            className="inline-flex min-h-11 items-center gap-2 whitespace-nowrap rounded-lg px-2 font-label-md text-label-md text-on-surface-variant hover:text-primary"
          >
            <Icon name="close" size={18} /> <span className="hidden sm:inline">Exit setup</span>
          </button>
        </div>

        {/* Stepper */}
        <ol className="mb-8 grid grid-cols-4 gap-2" aria-label="Campaign setup progress">
          {STEPS.map((s, i) => (
            <li key={s} className="min-w-0">
              <div
                className={`flex min-h-11 items-center gap-2 rounded-lg px-2 font-label-sm text-label-sm font-bold ${
                  i <= step
                    ? "bg-primary text-on-primary"
                    : "bg-surface-container-high text-on-surface-variant"
                }`}
                aria-current={i === step ? "step" : undefined}
              >
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface-container-lowest text-primary">
                  {i < step ? <Icon name="check" size={16} /> : i + 1}
                </span>
                <span className="hidden truncate sm:block">{s}</span>
              </div>
            </li>
          ))}
        </ol>

        <Card>
          {step === 0 && (
            <div className="flex flex-col gap-5">
              <div>
                <label className={label} htmlFor="offer">Product / Offer *</label>
                <input
                  id="offer"
                  className={field}
                  placeholder="e.g., Serum X"
                  value={offer}
                  onChange={(e) => setOffer(e.target.value)}
                  aria-required="true"
                  aria-invalid={Boolean(error && !offer.trim())}
                />
                <p className="mt-2 min-h-[1lh] font-label-sm text-label-sm text-on-surface-variant">
                  This exact offer becomes the shared context for concepts, scripts, creators and video.
                </p>
              </div>
            </div>
          )}

          {step === 1 && (
            <div className="flex flex-col gap-5">
              <div className="flex items-center gap-2 text-ai-processing">
                <Icon name="auto_awesome" />
                <span className="font-headline-md text-headline-md">Creative Direction</span>
              </div>
              <div>
                <label className={label} htmlFor="creator-prompt">Creator guidance</label>
                <textarea
                  id="creator-prompt"
                  className={field}
                  rows={4}
                  placeholder="Look, energy, wardrobe and setting…"
                  value={creatorPrompt}
                  onChange={(e) => setCreatorPrompt(e.target.value)}
                />
              </div>
              <div>
                <label className={label} htmlFor="video-prompt">Video guidance</label>
                <textarea
                  id="video-prompt"
                  className={field}
                  rows={4}
                  placeholder="Framing, camera motion and mood…"
                  value={videoPrompt}
                  onChange={(e) => setVideoPrompt(e.target.value)}
                />
              </div>
              <p className="font-label-sm text-label-sm text-on-surface-variant">
                Both fields are optional. Leave them blank to let the engine derive direction from the offer.
              </p>
            </div>
          )}

          {step === 2 && (
            <div className="flex flex-col gap-5">
              <div>
                <label className={label} htmlFor="batch-size">Batch size</label>
                <input
                  id="batch-size"
                  type="number"
                  min={1}
                  max={48}
                  className={field}
                  value={batch}
                  onChange={(e) => setBatch(Math.max(1, Number(e.target.value) || 1))}
                />
                <p className="mt-1 font-label-sm text-label-sm text-on-surface-variant">
                  Number of concepts the pipeline will fan out in parallel.
                </p>
              </div>
              <div>
                <label className={label} htmlFor="platform">Platform</label>
                <select
                  id="platform"
                  className={field}
                  value={platform}
                  onChange={(e) => setPlatform(e.target.value as (typeof PLATFORMS)[number])}
                >
                  {PLATFORMS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </div>
              <label className="flex min-h-11 items-start gap-3 rounded-lg border border-surface-border p-3 cursor-pointer">
                <input
                  type="checkbox"
                  className="mt-0.5 rounded border-surface-border text-primary focus:ring-primary"
                  checked={editConcepts}
                  onChange={(e) => setEditConcepts(e.target.checked)}
                />
                <span>
                  <span className="block font-body-md text-body-md text-primary">Review concepts before scripts</span>
                  <span className="block font-label-sm text-label-sm text-on-surface-variant">Pause after concepts so a person can edit or exclude them.</span>
                </span>
              </label>
              <label className="flex min-h-11 items-start gap-3 rounded-lg border border-surface-border p-3 cursor-pointer">
                <input
                  type="checkbox"
                  className="mt-0.5 rounded border-surface-border text-primary focus:ring-primary"
                  checked={approveCreators}
                  onChange={(e) => setApproveCreators(e.target.checked)}
                />
                <span>
                  <span className="block font-body-md text-body-md text-primary">Review creators before videos</span>
                  <span className="block font-label-sm text-label-sm text-on-surface-variant">Pause at the roster gate before the video stage starts.</span>
                </span>
              </label>
            </div>
          )}

          {step === 3 && (
            <div className="flex flex-col gap-3 font-body-md text-body-md">
              <div className="flex items-center gap-2 text-primary mb-2">
                <Icon name="fact_check" />
                <span className="font-headline-md text-headline-md">Review &amp; Launch</span>
              </div>
              {[
                ["Product / Offer", offer || "—"],
                ["Platform", platform],
                ["Batch size", String(batch)],
                ["Concept review", editConcepts ? "Yes (human gate)" : "No (continue automatically)"],
                ["Creator approval", approveCreators ? "Yes (human gate)" : "No (continue automatically)"],
                ["Creator guidance", creatorPrompt.trim() ? "Provided" : "Derived by engine"],
                ["Video guidance", videoPrompt.trim() ? "Provided" : "Derived by engine"],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between border-b border-surface-border py-2">
                  <span className="text-on-surface-variant">{k}</span>
                  <span className="text-primary font-medium">{v}</span>
                </div>
              ))}
              {error && <p role="alert" className="mt-2 font-label-md text-label-md text-error">{error}</p>}
            </div>
          )}
        </Card>

        {/* Nav */}
        <div className="flex items-center justify-between mt-6">
          <Button
            variant="ghost"
            onClick={() => (step === 0 ? navigate("/") : setStep((s) => s - 1))}
          >
            {step === 0 ? "Cancel" : "Back"}
          </Button>
          {step < STEPS.length - 1 ? (
            <Button icon="arrow_forward" disabled={!canContinue} onClick={() => setStep((s) => s + 1)}>
              Continue to {STEPS[step + 1]}
            </Button>
          ) : (
            <Button icon="rocket_launch" loading={submitting} onClick={launch}>
              {submitting ? "Launching" : "Launch Campaign"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
