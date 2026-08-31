import { useState } from "react";
import { useNavigate } from "react-router";
import { useRetryRunMutation, useSessionQuery } from "../api/queries";
import { Button } from "./Button";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

export function RetryCampaignButton({
  runId,
  variant = "primary",
  className = "",
  showError = true,
}: {
  runId: string;
  variant?: ButtonVariant;
  className?: string;
  showError?: boolean;
}) {
  const navigate = useNavigate();
  const retryRun = useRetryRunMutation();
  const session = useSessionQuery();
  const canRetry = session.data ? session.data.permissions.includes("runs:retry") : false;
  const [error, setError] = useState<string | null>(null);

  if (!canRetry) return null;

  async function retry() {
    setError(null);
    try {
      const retried = await retryRun.mutateAsync(runId);
      navigate(`/campaigns/${encodeURIComponent(retried.run_id)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not retry this campaign.");
    }
  }

  return (
    <div className={className}>
      <Button loading={retryRun.isPending} variant={variant} icon="refresh" onClick={retry}>
        Retry campaign
      </Button>
      {showError && error && (
        <p role="alert" className="mt-2 max-w-sm font-label-sm text-label-sm text-error">
          {error}
        </p>
      )}
    </div>
  );
}
