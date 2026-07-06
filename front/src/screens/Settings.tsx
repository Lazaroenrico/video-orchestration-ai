import { PageHeader } from "../components/PageHeader";
import { Card, SectionTitle } from "../components/Card";
import { Icon } from "../components/Icon";
import { Button } from "../components/Button";
import { useAsync } from "../api/useAsync";
import { api } from "../api/client";

function StoreRow({ label, path, exists }: { label: string; path?: string; exists?: boolean }) {
  return (
    <div className="flex items-center justify-between py-3 border-b border-surface-border last:border-0">
      <div>
        <div className="font-body-md text-body-md text-primary">{label}</div>
        <div className="font-mono text-label-sm text-label-sm text-on-surface-variant break-all">
          {path ?? "—"}
        </div>
      </div>
      <span
        className={`inline-flex items-center gap-1 font-label-sm text-label-sm ${
          exists ? "text-success-published" : "text-on-surface-variant"
        }`}
      >
        <Icon name={exists ? "check_circle" : "remove_circle_outline"} size={16} />
        {exists ? "Present" : "Not created"}
      </span>
    </div>
  );
}

export function Settings() {
  const prompts = useAsync(() => api.getPrompts(), []);
  const creators = useAsync(() => api.getCreators(), []);

  return (
    <div>
      <PageHeader title="Workspace Settings" subtitle="Configure your workspace and inspect local stores." />

      <div className="grid grid-cols-12 gap-gutter">
        <div className="col-span-12 lg:col-span-7">
          <Card className="mb-gutter">
            <SectionTitle title="Local Stores" />
            <StoreRow
              label="Prompt templates"
              path={prompts.data?.store_path}
              exists={prompts.data?.exists}
            />
            <StoreRow
              label="Creators library"
              path={creators.data?.store_path}
              exists={creators.data?.exists}
            />
          </Card>

          <Card>
            <SectionTitle title="Preferences" />
            {[
              ["Default platform", "TikTok"],
              ["Default batch size", "6"],
              ["Human approval gate", "Enabled"],
              ["Dry-run mode", "Config-driven (providers.yaml)"],
            ].map(([k, v]) => (
              <div key={k} className="flex items-center justify-between py-3 border-b border-surface-border last:border-0">
                <span className="font-body-md text-body-md text-on-surface-variant">{k}</span>
                <span className="font-body-md text-body-md text-primary font-medium">{v}</span>
              </div>
            ))}
            <p className="mt-3 font-label-sm text-label-sm text-on-surface-variant">
              Preferences shown for reference — persisted workspace settings are not yet backed by an API.
            </p>
          </Card>
        </div>

        <div className="col-span-12 lg:col-span-5">
          <Card className="bg-ai-processing/5 border-ai-processing/20">
            <div className="flex items-center gap-2 mb-2 text-ai-processing">
              <Icon name="workspaces" />
              <span className="font-headline-md text-headline-md">Pro Workspace</span>
            </div>
            <p className="font-body-md text-body-md text-on-surface-variant mb-4">
              Marketing Suite · AI UGC Orchestrator. Runtime adapters and credits are managed in the
              Integrations hub.
            </p>
            <Button variant="secondary" icon="extension" className="w-full">
              Manage Integrations
            </Button>
          </Card>
        </div>
      </div>
    </div>
  );
}
