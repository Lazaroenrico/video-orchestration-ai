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
    <div className="inline-flex items-center gap-2 rounded-lg border border-surface-border bg-surface-container-lowest px-3 py-1.5">
      <Icon name="tune" size={16} className="text-on-surface-variant" />
      <select
        value={selected ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="border-0 bg-transparent font-label-md text-label-md text-primary focus:ring-0 p-0 pr-6"
      >
        {runs.map((id) => (
          <option key={id} value={id}>
            {shortRun(id)}
            {active.has(id) ? " · live" : ""}
          </option>
        ))}
      </select>
    </div>
  );
}
