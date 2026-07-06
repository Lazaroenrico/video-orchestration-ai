export function ProgressBar({
  value,
  tone = "primary",
  className = "",
}: {
  value: number; // 0..100
  tone?: "primary" | "processing" | "success" | "warning";
  className?: string;
}) {
  const pct = Math.max(0, Math.min(100, value));
  const fill = {
    primary: "bg-primary",
    processing: "bg-ai-processing",
    success: "bg-success-published",
    warning: "bg-warning-review",
  }[tone];
  return (
    <div className={`h-2 w-full rounded-full bg-surface-container-high overflow-hidden ${className}`}>
      <div className={`h-full rounded-full ${fill} transition-[width] duration-500`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function StatTile({
  label,
  value,
  hint,
  hintTone = "success",
}: {
  label: string;
  value: string | number;
  hint?: string;
  hintTone?: "success" | "error" | "muted";
}) {
  const hintCls = {
    success: "text-success-published",
    error: "text-error",
    muted: "text-on-surface-variant",
  }[hintTone];
  return (
    <div className="bg-surface-container-lowest border border-surface-border p-4 rounded-xl flex flex-col gap-1 shadow-[0px_4px_12px_rgba(0,0,0,0.03)]">
      <span className="font-label-sm text-label-sm text-on-surface-variant uppercase tracking-wider">
        {label}
      </span>
      <div className="flex items-baseline gap-2">
        <span className="font-headline-md text-headline-md text-primary">{value}</span>
        {hint && <span className={`font-label-sm text-label-sm ${hintCls}`}>{hint}</span>}
      </div>
    </div>
  );
}
