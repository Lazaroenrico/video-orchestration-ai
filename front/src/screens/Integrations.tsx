import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Icon } from "../components/Icon";
import { Loading, ErrorState, EmptyState } from "../components/States";
import { useAsync } from "../api/useAsync";
import { api } from "../api/client";

const STAGE_META: Record<string, { label: string; icon: string; blurb: string }> = {
  llm: { label: "LLM", icon: "smart_toy", blurb: "Concept & script generation." },
  creator: { label: "Creator", icon: "face", blurb: "Persona image + voice synthesis." },
  video: { label: "Video", icon: "movie", blurb: "Talking-head video generation." },
  qc: { label: "Quality Control", icon: "verified", blurb: "Automated media integrity checks." },
  assembly: { label: "Assembly", icon: "auto_awesome_motion", blurb: "Final cut composition." },
  upscale: { label: "Video Upscale", icon: "high_quality", blurb: "Final-video enhancement after assembly." },
  judge: { label: "Judge", icon: "gavel", blurb: "LLM evaluation gateway." },
};

const isMock = (adapter: string) => adapter === "mock";

export function Integrations() {
  const { data, loading, error } = useAsync(() => api.getIntegrations(), []);
  const stages = data ? Object.entries(data.stages) : [];

  return (
    <div>
      <PageHeader
        title="Integrations"
        subtitle="Read-only view of the adapters configured for each pipeline responsibility."
      />

      {loading && <Loading />}
      {error && <ErrorState message={error} />}
      {!loading && !error && stages.length === 0 && (
        <EmptyState icon="extension" title="No providers configured" hint="providers.yaml has no adapters mapped." />
      )}

      {!loading && !error && stages.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-gutter">
          {stages.map(([stage, adapter]) => {
            const meta = STAGE_META[stage] ?? { label: stage, icon: "extension", blurb: "Pipeline stage." };
            const mock = isMock(adapter);
            return (
              <Card key={stage} className="flex flex-col">
                <div className={`-m-6 mb-4 h-1 rounded-t-xl ${mock ? "bg-surface-container-high" : "bg-success-published"}`} />
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-lg bg-surface-container flex items-center justify-center text-primary">
                      <Icon name={meta.icon} />
                    </div>
                    <div>
                      <div className="font-headline-md text-headline-md text-primary">{meta.label}</div>
                      <div
                        className={`font-label-sm text-label-sm uppercase tracking-wider ${
                          mock ? "text-on-surface-variant" : "text-success-published"
                        }`}
                      >
                        {mock ? "Dry-run / mock" : "Configured"}
                      </div>
                    </div>
                  </div>
                </div>
                <p className="font-body-md text-body-md text-on-surface-variant flex-1">{meta.blurb}</p>
                <div className="mt-4 flex items-center justify-between gap-3 border-t border-surface-border pt-3">
                  <span className="font-mono text-label-sm text-label-sm text-on-surface-variant">
                    {adapter}
                  </span>
                  <span className="text-right font-label-sm text-label-sm text-on-surface-variant">Managed by config</span>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
