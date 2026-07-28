import { Icon } from "./Icon";
import { shortRun } from "../lib/format";

export function RunSelect({
  runs,
  active,
  selected,
  onChange,
}: {
  runs: string[];
  active: Set<string>;
  selected: string | null;
  onChange: (id: string) => void;
}) {
  if (runs.length === 0) return null;
  return (
    <label className="inline-flex min-h-11 max-w-full items-center gap-2 rounded-lg border border-surface-border bg-surface-container-lowest px-3 py-1.5">
      <Icon name="tune" size={16} className="text-on-surface-variant" />
      <select
        value={selected ?? ""}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Select campaign run"
        className="min-w-0 border-0 bg-transparent p-0 pr-6 font-label-md text-label-md text-primary focus:ring-0"
      >
        {runs.map((id) => (
          <option key={id} value={id}>
            {shortRun(id)}
            {active.has(id) ? " · live" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}
