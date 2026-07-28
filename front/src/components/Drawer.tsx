import { useEffect, useId, useRef } from "react";
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
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      className="m-0 ml-auto h-[100dvh] max-h-none w-full max-w-[420px] border-0 bg-transparent p-0 backdrop:bg-black/30 backdrop:backdrop-blur-sm"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === dialogRef.current) onClose();
      }}
    >
      <aside className="flex h-full flex-col border-l border-surface-border bg-surface-container-lowest shadow-2xl">
        <header className="flex h-16 items-center justify-between border-b border-surface-border px-6">
          <div id={titleId} className="font-headline-md text-headline-md text-primary">{title}</div>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg text-on-surface-variant hover:text-primary"
            aria-label="Close panel"
          >
            <Icon name="close" />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto p-6">{children}</div>
        {footer && <footer className="p-4 border-t border-surface-border">{footer}</footer>}
      </aside>
    </dialog>
  );
}
