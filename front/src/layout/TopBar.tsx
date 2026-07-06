import { useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon";

export function TopBar() {
  const navigate = useNavigate();
  return (
    <header className="flex justify-between items-center h-16 px-margin-desktop w-full bg-surface/80 backdrop-blur-md sticky top-0 z-40 border-b border-surface-border shadow-sm">
      <div className="flex items-center gap-6">
        <span className="font-headline-md text-headline-md font-black text-primary">
          Orchestrator AI
        </span>
      </div>
      <div className="flex items-center gap-6">
        <nav className="flex gap-4">
          <span className="text-on-surface-variant font-label-md text-label-md">
            Credits: 1,240
          </span>
        </nav>
        <div className="flex items-center gap-4 border-l border-surface-border pl-4">
          <button className="text-on-surface-variant hover:text-primary transition-colors flex items-center justify-center">
            <Icon name="notifications" />
          </button>
          <button className="text-on-surface-variant hover:text-primary transition-colors flex items-center justify-center">
            <Icon name="help_outline" />
          </button>
          <div className="w-8 h-8 rounded-full overflow-hidden border border-surface-border cursor-pointer bg-surface-container flex items-center justify-center">
            <Icon name="person" size={18} className="text-on-surface-variant" />
          </div>
          <button
            onClick={() => navigate("/campaigns/new")}
            className="bg-primary text-on-primary font-label-md text-label-md px-4 py-1.5 rounded flex items-center gap-2 hover:bg-surface-tint transition-colors font-bold"
          >
            New Campaign
          </button>
        </div>
      </div>
    </header>
  );
}
