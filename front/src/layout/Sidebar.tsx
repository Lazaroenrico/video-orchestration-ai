import { useEffect, useRef } from "react";
import { NavLink, useNavigate } from "react-router";
import { Icon } from "../components/Icon";

type NavItem = { label: string; icon: string; to: string; end?: boolean };

const NAV: NavItem[] = [
  { label: "Dashboard", icon: "dashboard", to: "/", end: true },
  { label: "Campaigns", icon: "campaign", to: "/campaigns" },
  { label: "Creators", icon: "groups", to: "/creators" },
  { label: "Concepts", icon: "description", to: "/scripts" },
  { label: "Video Review", icon: "movie", to: "/review" },
  { label: "Operations", icon: "hourglass_empty", to: "/queue" },
  { label: "Analytics", icon: "analytics", to: "/analytics" },
  { label: "Integrations", icon: "extension", to: "/integrations" },
  { label: "Workspace", icon: "settings", to: "/settings" },
];

const baseLink =
  "flex min-h-11 items-center gap-3 rounded-lg px-3 py-2 font-label-md text-label-md whitespace-nowrap transition-[background-color,color,transform] duration-150 active:translate-y-px";

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const navigate = useNavigate();
  return (
    <>
      <div className="mb-7 mt-1 flex items-center gap-3 px-2">
        <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg border border-surface-border bg-surface-container text-primary">
          <Icon name="motion_photos_on" size={22} />
        </div>
        <div className="min-w-0">
          <span className="block truncate font-headline-md text-headline-md font-bold text-primary">
            Marketing Suite
          </span>
          <span className="block truncate font-label-sm text-label-sm text-on-surface-variant">
            AI UGC workspace
          </span>
        </div>
      </div>

      <button
        type="button"
        onClick={() => {
          onNavigate?.();
          navigate("/campaigns/new");
        }}
        className="mb-6 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 py-3 font-label-md text-label-md font-bold text-on-primary transition-transform duration-150 active:translate-y-px"
      >
        <Icon name="add" size={18} />
        New Campaign
      </button>

      <div className="flex flex-1 flex-col gap-1 overflow-y-auto">
        <p className="px-3 pb-1 font-label-sm text-label-sm uppercase tracking-wider text-on-surface-variant">
          Workspace
        </p>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            className={({ isActive }) =>
              `${baseLink} ${
                isActive
                  ? "bg-surface-container-high text-primary font-bold"
                  : "text-on-surface-variant hover:bg-surface-container-low hover:text-primary"
              }`
            }
          >
            {({ isActive }) => (
              <>
                <Icon name={item.icon} fill={isActive} />
                {item.label}
              </>
            )}
          </NavLink>
        ))}
      </div>

      <div className="mt-4 border-t border-surface-border pt-4">
        <p className="px-3 font-label-sm text-label-sm leading-relaxed text-on-surface-variant">
          v1 ends at assembled output.
        </p>
      </div>
    </>
  );
}

function MobileSidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      className="mobile-nav-dialog lg:hidden"
      aria-label="Primary navigation"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === dialogRef.current) onClose();
      }}
    >
      <nav className="flex h-full flex-col bg-surface p-4">
        <div className="mb-2 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg text-on-surface-variant"
            aria-label="Close navigation"
            autoFocus={open}
          >
            <Icon name="close" />
          </button>
        </div>
        <SidebarContent onNavigate={onClose} />
      </nav>
    </dialog>
  );
}

export function Sidebar({ mobileOpen, onClose }: { mobileOpen: boolean; onClose: () => void }) {
  return (
    <>
      <nav className="fixed inset-y-0 left-0 z-50 hidden w-[240px] flex-col border-r border-surface-border bg-surface p-4 lg:flex" aria-label="Primary navigation">
        <SidebarContent />
      </nav>
      <MobileSidebar open={mobileOpen} onClose={onClose} />
    </>
  );
}
