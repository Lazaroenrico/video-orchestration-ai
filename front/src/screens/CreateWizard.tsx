import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { api } from "../api/client";

const PLATFORMS = ["tiktok", "instagram", "youtube"] as const;
const OBJECTIVES = ["Conversions", "Awareness", "Engagement", "Lead Generation"];
const CHANNELS = ["LinkedIn", "Twitter / X", "Instagram", "Email Newsletter"];
const STEPS = ["Brief", "Direction", "Settings", "Review"];

const label =
  "block font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant mb-1";
const field =
  "w-full rounded-lg border-surface-border bg-surface-container-lowest font-body-md text-body-md focus:ring-primary focus:border-primary";

export function CreateWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [offer, setOffer] = useState("");
  const [objective, setObjective] = useState("");
  const [audience, setAudience] = useState("");
  const [channels, setChannels] = useState<string[]>(["LinkedIn"]);
  const [creatorPrompt, setCreatorPrompt] = useState("");
  const [videoPrompt, setVideoPrompt] = useState("");
  const [batch, setBatch] = useState(6);
  const [platform, setPlatform] = useState<(typeof PLATFORMS)[number]>("tiktok");
  const [approveCreators, setApproveCreators] = useState(true);

  const toggleChannel = (c: string) =>
    setChannels((cur) => (cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c]));

  const canContinue = step === 0 ? offer.trim().length > 0 : true;

  async function launch() {
    setSubmitting(true);
    setError(null);
    try {
      const { run_id } = await api.startRun({
        offer: offer.trim() || name.trim() || "untitled campaign",
        batch,
        platform,
        creator_prompt: creatorPrompt.trim() || null,
        video_prompt: videoPrompt.trim() || null,
        approve_creators: approveCreators,
      });
      navigate(`/campaigns/${run_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen bg-background p-margin-desktop">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-start justify-between mb-gutter">
          <div>
            <h1 className="font-headline-lg text-headline-lg text-primary mb-1">
              Create New Campaign
            </h1>
            <p className="font-body-md text-body-md text-on-surface-variant">
              Configure your orchestration pipeline parameters.
            </p>
          </div>
          <button
            onClick={() => navigate("/")}
            className="flex items-center gap-2 text-on-surface-variant hover:text-primary font-label-md text-label-md"
          >
            <Icon name="close" size={18} /> Exit Setup
          </button>
        </div>

        {/* Stepper */}
        <div className="flex items-center gap-2 mb-8">
          {STEPS.map((s, i) => (
            <div key={s} className="flex items-center gap-2 flex-1">
              <div
                className={`w-7 h-7 rounded-full flex items-center justify-center font-label-sm text-label-sm font-bold ${
                  i <= step
                    ? "bg-primary text-on-primary"
                    : "bg-surface-container-high text-on-surface-variant"
                }`}
              >
                {i < step ? <Icon name="check" size={16} /> : i + 1}
              </div>
              <span
                className={`font-label-md text-label-md ${
                  i <= step ? "text-primary" : "text-on-surface-variant"
                }`}
              >
                {s}
              </span>
              {i < STEPS.length - 1 && <div className="flex-1 h-px bg-surface-border" />}
            </div>
          ))}
        </div>

        <Card>
          {step === 0 && (
            <div className="flex flex-col gap-5">
              <div>
                <label className={label}>Campaign Name</label>
                <input
                  className={field}
                  placeholder="e.g., Q3 Product Launch"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div>
                <label className={label}>Product / Offer *</label>
                <input
                  className={field}
                  placeholder="e.g., Serum X"
                  value={offer}
                  onChange={(e) => setOffer(e.target.value)}
                />
              </div>
              <div>
                <label className={label}>Primary Objective</label>
                <select className={field} value={objective} onChange={(e) => setObjective(e.target.value)}>
                  <option value="">Select objective…</option>
                  {OBJECTIVES.map((o) => (
                    <option key={o}>{o}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className={label}>Audience Description</label>
                <textarea
                  className={field}
                  rows={3}
                  placeholder="Describe your target demographic in detail…"
                  value={audience}
                  onChange={(e) => setAudience(e.target.value)}
                />
                <p className="mt-1 font-label-sm text-label-sm text-on-surface-variant">
                  Used to calibrate generated copy tone.
                </p>
              </div>
              <div>
                <label className={label}>Distribution Channels</label>
                <div className="flex flex-wrap gap-2">
                  {CHANNELS.map((c) => (
                    <button
                      key={c}
                      onClick={() => toggleChannel(c)}
                      className={`px-3 py-1.5 rounded-full font-label-md text-label-md border ${
                        channels.includes(c)
                          ? "bg-primary text-on-primary border-primary"
                          : "bg-surface-container-lowest text-on-surface-variant border-surface-border"
                      }`}
                    >
                      {channels.includes(c) && <Icon name="check" size={14} className="mr-1 align-middle" />}
                      {c}
                    </button>
                  ))}
                </div>
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
                <label className={label}>Creator Prompt</label>
                <textarea
                  className={field}
                  rows={4}
                  placeholder="Describe the on-camera persona: look, energy, wardrobe, setting…"
                  value={creatorPrompt}
                  onChange={(e) => setCreatorPrompt(e.target.value)}
                />
              </div>
              <div>
                <label className={label}>Video Prompt</label>
                <textarea
                  className={field}
                  rows={4}
                  placeholder="Describe the talking-head shot: framing, camera motion, mood…"
                  value={videoPrompt}
                  onChange={(e) => setVideoPrompt(e.target.value)}
                />
              </div>
              <p className="font-label-sm text-label-sm text-on-surface-variant">
                Leave blank to let the engine derive prompts from the brief.
              </p>
            </div>
          )}

          {step === 2 && (
            <div className="flex flex-col gap-5">
              <div>
                <label className={label}>Batch Size</label>
                <input
                  type="number"
                  min={1}
                  max={48}
                  className={field}
                  value={batch}
                  onChange={(e) => setBatch(Math.max(1, Number(e.target.value) || 1))}
                />
                <p className="mt-1 font-label-sm text-label-sm text-on-surface-variant">
                  Number of concepts fanned out in parallel.
                </p>
              </div>
              <div>
                <label className={label}>Platform</label>
                <select
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
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  className="rounded border-surface-border text-primary focus:ring-primary"
                  checked={approveCreators}
                  onChange={(e) => setApproveCreators(e.target.checked)}
                />
                <span className="font-body-md text-body-md text-primary">
                  Review &amp; approve creators before generating videos
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
                ["Campaign", name || "—"],
                ["Product / Offer", offer || "—"],
                ["Objective", objective || "—"],
                ["Platform", platform],
                ["Batch size", String(batch)],
                ["Approve creators", approveCreators ? "Yes (human gate)" : "No (auto)"],
                ["Channels", channels.join(", ") || "—"],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between border-b border-surface-border py-2">
                  <span className="text-on-surface-variant">{k}</span>
                  <span className="text-primary font-medium">{v}</span>
                </div>
              ))}
              {error && <p className="text-error font-label-md text-label-md mt-2">{error}</p>}
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
            <Button icon="rocket_launch" disabled={submitting} onClick={launch}>
              {submitting ? "Launching…" : "Launch Campaign"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
