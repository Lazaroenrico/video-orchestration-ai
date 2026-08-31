import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import { api } from "../api/client";
import type { UserSession } from "../api/contracts";

describe("Sidebar Component", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders logout link and new campaign button for member in cloudflare_access mode", async () => {
    const mockSession: UserSession = {
      id: "user-1",
      subject: "access|member",
      email: "member@example.com",
      display_name: "Member User",
      organization: {
        id: "org-1",
        slug: "acme",
        name: "Acme Corp",
      },
      role: "member",
      permissions: ["read", "runs:create", "runs:review"],
      auth_mode: "cloudflare_access",
    };
    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Sidebar mobileOpen={false} onClose={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    const names = await screen.findAllByText("Member User");
    expect(names.length).toBeGreaterThan(0);

    const newCampaignBtns = screen.getAllByRole("button", { name: /New Campaign/i });
    expect(newCampaignBtns.length).toBeGreaterThan(0);

    const logoutLinks = screen.getAllByTitle("Logout");
    expect(logoutLinks.length).toBeGreaterThan(0);
    expect(logoutLinks[0]).toHaveAttribute("href", "/cdn-cgi/access/logout");
  });

  it("hides logout link in disabled auth mode and hides new campaign button for viewer", async () => {
    const mockSession: UserSession = {
      id: "user-2",
      subject: "local-viewer",
      email: "viewer@local.test",
      display_name: "Local Viewer",
      organization: {
        id: "org-1",
        slug: "local",
        name: "Local Organization",
      },
      role: "viewer",
      permissions: ["read"],
      auth_mode: "disabled",
    };
    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Sidebar mobileOpen={false} onClose={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    const names = await screen.findAllByText("Local Viewer");
    expect(names.length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /New Campaign/i })).not.toBeInTheDocument();
    expect(screen.queryByTitle("Logout")).not.toBeInTheDocument();
  });
});
