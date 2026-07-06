import { useEffect, useState } from "react";
import { useAsync } from "./useAsync";
import { api } from "./client";

/**
 * Load the run index and track a selected run id, defaulting to the first active
 * run (live SSE) or the most recent known run. Powers the Queue/Review/Concepts
 * screens, which all attach to one run's event stream.
 */
export function useRunSelection() {
  const { data, loading, error } = useAsync(() => api.getRuns(), []);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (!data || selected) return;
    setSelected(data.active[0] ?? data.runs[0] ?? null);
  }, [data, selected]);

  const runs = data?.runs ?? [];
  const active = new Set(data?.active ?? []);
  return { runs, active, selected, setSelected, loading, error };
}
