import { useState } from "react";
import { Outlet } from "react-router";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

export function AppShell() {
  const [navOpen, setNavOpen] = useState(false);
  return (
    <div className="min-h-[100dvh] bg-background font-body-md text-body-md text-on-surface">
      <Sidebar mobileOpen={navOpen} onClose={() => setNavOpen(false)} />
      <div className="flex min-h-[100dvh] flex-col lg:ml-[240px]">
        <TopBar onMenu={() => setNavOpen(true)} />
        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-margin-desktop lg:py-10">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
