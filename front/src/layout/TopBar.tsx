import { useNavigate } from "react-router";
import { Icon } from "../components/Icon";

export function TopBar({ onMenu }: { onMenu: () => void }) {
  const navigate = useNavigate();
  return (
    <header className="sticky top-0 z-40 flex h-16 w-full items-center justify-between border-b border-surface-border bg-surface/90 px-4 backdrop-blur-md sm:px-6 lg:px-margin-desktop">
      <div className="flex min-w-0 items-center gap-3">
        <button
          type="button"
          onClick={onMenu}
          className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg text-on-surface-variant lg:hidden"
          aria-label="Open navigation"
        >
          <Icon name="menu" />
        </button>
        <div className="min-w-0">
          <span className="block truncate font-headline-md text-headline-md font-bold text-primary">
            Orchestrator AI
          </span>
          <span className="hidden font-label-sm text-label-sm text-on-surface-variant sm:block">
            Production workspace
          </span>
        </div>
      </div>
      <button
        type="button"
        onClick={() => navigate("/campaigns/new")}
        className="inline-flex min-h-11 items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-primary px-3 font-label-md text-label-md font-bold text-on-primary transition-transform duration-150 active:translate-y-px sm:px-4"
      >
        <Icon name="add" size={18} />
        <span className="hidden sm:inline">New Campaign</span>
        <span className="sm:hidden">New</span>
      </button>
    </header>
  );
}
