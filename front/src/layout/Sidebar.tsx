import { NavLink, useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon";

type NavItem = { label: string; icon: string; to: string; end?: boolean };

const NAV: NavItem[] = [
  { label: "Dashboard", icon: "dashboard", to: "/", end: true },
  { label: "Campaigns", icon: "campaign", to: "/campaigns" },
  { label: "Creators", icon: "groups", to: "/creators" },
  { label: "Scripts", icon: "description", to: "/scripts" },
  { label: "Videos", icon: "movie", to: "/review" },
  { label: "Queue", icon: "hourglass_empty", to: "/queue" },
  { label: "Publishing", icon: "publish", to: "/publishing" },
  { label: "Analytics", icon: "analytics", to: "/analytics" },
  { label: "Integrations", icon: "extension", to: "/integrations" },
  { label: "Settings", icon: "settings", to: "/settings" },
];

const baseLink =
  "flex items-center gap-3 px-3 py-2 rounded-lg font-label-md text-label-md transition-colors duration-150";

export function Sidebar() {
  const navigate = useNavigate();
  return (
    <nav className="w-[240px] h-screen fixed left-0 top-0 border-r border-surface-border bg-surface flex flex-col p-4 gap-2 z-50">
      <div className="mb-8 mt-2 px-2 flex items-center gap-3">
        <div className="w-10 h-10 rounded-lg overflow-hidden bg-surface-container flex-shrink-0 border border-surface-border flex items-center justify-center">
          <Icon name="motion_photos_on" className="text-primary" size={22} />
        </div>
        <div className="flex flex-col">
          <span className="font-headline-md text-headline-md font-bold text-primary truncate">
            Marketing Suite
          </span>
          <span className="font-label-md text-label-md text-on-surface-variant truncate">
            Pro Workspace
          </span>
        </div>
      </div>

      <button
        onClick={() => navigate("/campaigns/new")}
        className="w-full bg-primary text-on-primary font-label-md text-label-md py-3 px-4 rounded-lg flex items-center justify-center gap-2 mb-6 hover:bg-surface-tint transition-colors duration-150 active:scale-95 shadow-[0px_4px_12px_rgba(0,0,0,0.03)] font-bold"
      >
        <Icon name="add" size={18} />
        New Campaign
      </button>

      <div className="flex-1 overflow-y-auto flex flex-col gap-1">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              `${baseLink} ${
                isActive
                  ? "text-primary font-bold bg-surface-container-high"
                  : "text-on-surface-variant hover:bg-surface-container-low"
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

      <div className="mt-auto pt-4 border-t border-surface-border flex flex-col gap-1">
        <a
          className={`${baseLink} text-on-surface-variant hover:bg-surface-container-low`}
          href="#"
        >
          <Icon name="swap_horiz" />
          Workspace Switcher
        </a>
      </div>
    </nav>
  );
}
