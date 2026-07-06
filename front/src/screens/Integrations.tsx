import { PageHeader } from "../components/PageHeader";
import { Card } from "../components/Card";
import { Button } from "../components/Button";
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
        subtitle="Connect external providers to enable AI synthesis and content distribution across your marketing stack."
        actions={<Button variant="secondary" icon="description">View Logs</Button>}
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
                <div className="h-1 -m-6 mb-4 rounded-t-xl" style={{ background: mock ? "#cfc4c5" : "#10b981" }} />
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
                        {mock ? "● Mock / Dry-run" : "● Connected"}
                      </div>
                    </div>
                  </div>
                </div>
                <p className="font-body-md text-body-md text-on-surface-variant flex-1">{meta.blurb}</p>
                <div className="mt-4 flex items-center justify-between">
                  <span className="font-mono text-label-sm text-label-sm text-on-surface-variant">
                    {adapter}
                  </span>
                  <div className="flex gap-2">
                    <Button variant="secondary" className="!py-1.5">Settings</Button>
                    <Button variant="secondary" icon="bolt" className="!py-1.5">Test</Button>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
