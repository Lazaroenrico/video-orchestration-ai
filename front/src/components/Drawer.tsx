import type { ReactNode } from "react";
import { Icon } from "./Icon";

// Right-side slide-over used by the Creators library and Queue detail panels.
export function Drawer({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[60] flex justify-end">
      <div className="absolute inset-0 bg-black/30 backdrop-blur-sm" onClick={onClose} />
      <aside className="relative w-[420px] max-w-full h-full bg-surface-container-lowest border-l border-surface-border flex flex-col shadow-2xl">
        <header className="flex items-center justify-between px-6 h-16 border-b border-surface-border">
          <div className="font-headline-md text-headline-md text-primary">{title}</div>
          <button
            onClick={onClose}
            className="text-on-surface-variant hover:text-primary transition-colors"
          >
            <Icon name="close" />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto p-6">{children}</div>
        {footer && <footer className="p-4 border-t border-surface-border">{footer}</footer>}
      </aside>
    </div>
  );
}
