import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

export function AppShell() {
  return (
    <div className="antialiased min-h-screen flex font-body-md text-body-md bg-background text-on-surface">
      <Sidebar />
      <div className="flex-1 ml-[240px] flex flex-col min-h-screen">
        <TopBar />
        <main className="flex-1 p-margin-desktop overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
