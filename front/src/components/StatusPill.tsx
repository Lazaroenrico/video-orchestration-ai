import { Icon } from "./Icon";

// Semantic pipeline states mapped to the Kinetic Command accent palette.
export type Status =
  | "generating"
  | "processing"
  | "review"
  | "approved"
  | "published"
  | "draft"
  | "failed"
  | "done";

const STYLES: Record<Status, { cls: string; icon?: string; label: string }> = {
  generating: { cls: "bg-ai-processing/10 text-ai-processing", icon: "bolt", label: "Generating" },
  processing: { cls: "bg-ai-processing/10 text-ai-processing", icon: "sync", label: "Processing" },
  review: { cls: "bg-warning-review/10 text-warning-review", icon: "rate_review", label: "In Review" },
  approved: { cls: "bg-success-published/10 text-success-published", icon: "check_circle", label: "Approved" },
  published: { cls: "bg-success-published/10 text-success-published", icon: "check_circle", label: "Published" },
  draft: { cls: "bg-draft-gray/10 text-draft-gray", icon: "edit_note", label: "Draft" },
  failed: { cls: "bg-error/10 text-error", icon: "error", label: "Failed" },
  done: { cls: "bg-success-published/10 text-success-published", icon: "check", label: "Done" },
};

export function StatusPill({ status, label }: { status: Status; label?: string }) {
  const s = STYLES[status];
  return (
    <span
      className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full font-label-sm text-label-sm font-medium ${s.cls}`}
    >
      {s.icon && <Icon name={s.icon} size={14} />}
      {label ?? s.label}
    </span>
  );
}
