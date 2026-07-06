import { Icon } from "./Icon";
import { Card } from "./Card";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <Card className="flex items-center gap-3 text-on-surface-variant">
      <Icon name="progress_activity" className="animate-spin" />
      <span className="font-body-md text-body-md">{label}</span>
    </Card>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <Card className="flex items-center gap-3 text-error border-error/30">
      <Icon name="error" />
      <span className="font-body-md text-body-md">{message}</span>
    </Card>
  );
}

export function EmptyState({
  icon = "inbox",
  title,
  hint,
  action,
}: {
  icon?: string;
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <Card className="flex flex-col items-center justify-center text-center gap-2 py-12 text-on-surface-variant">
      <Icon name={icon} size={40} className="text-surface-tint" />
      <div className="font-headline-md text-headline-md text-primary">{title}</div>
      {hint && <p className="font-body-md text-body-md max-w-md">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </Card>
  );
}
