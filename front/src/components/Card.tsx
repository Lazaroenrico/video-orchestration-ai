import type { ReactNode } from "react";

const SHADOW = "shadow-[0px_4px_12px_rgba(0,0,0,0.03)]";

export function Card({
  children,
  className = "",
  padded = true,
}: {
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <div
      className={`min-w-0 bg-surface-container-lowest border border-surface-border rounded-xl ${SHADOW} ${
        padded ? "p-6" : ""
      } ${className}`}
    >
      {children}
    </div>
  );
}

export function SectionTitle({
  title,
  action,
}: {
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2 mb-4 sm:flex-row sm:items-center sm:justify-between">
      <h2 className="font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
        {title}
      </h2>
      {action}
    </div>
  );
}
