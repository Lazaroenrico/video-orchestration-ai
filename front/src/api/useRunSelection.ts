import { useEffect, useRef, useState } from "react";
import { useAsync } from "./useAsync";
import { api } from "./client";

/**
 * Load the run index and track a selected run id, defaulting to the first active
 * run (live SSE) or the most recent known run. Powers the Queue/Review/Concepts
 * screens, which all attach to one run's event stream.
 */
export function useRunSelection(preferredRunId?: string | null) {
  const { data, loading, error } = useAsync(() => api.getRuns(), []);
  const preferred = preferredRunId?.trim() || null;
  const [selected, setSelected] = useState<string | null>(() => preferred);
  const lastAppliedPreferred = useRef<string | null>(preferred);

  useEffect(() => {
    if (preferred && preferred !== lastAppliedPreferred.current) {
      setSelected(preferred);
    }
    lastAppliedPreferred.current = preferred;
  }, [preferred]);

  useEffect(() => {
    if (!data || selected) return;
    setSelected(data.active[0] ?? data.runs[0] ?? null);
  }, [data, selected]);

  const baseRuns = data?.runs ?? [];
  const runs = selected && !baseRuns.includes(selected) ? [selected, ...baseRuns] : baseRuns;
  const active = new Set(data?.active ?? []);
  return { runs, active, selected, setSelected, loading, error };
}
