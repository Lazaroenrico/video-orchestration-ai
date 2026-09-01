import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { Settings } from "./Settings";
import { api } from "../api/client";
import type { UserSession, MemberRecord } from "../api/contracts";

describe("Settings Component", () => {
  it("renders member management for owner and allows adding a member", async () => {
    const mockSession: UserSession = {
      id: "user-owner",
      subject: "access|owner",
      email: "owner@acme.com",
      display_name: "Owner User",
      organization: {
        id: "org-1",
        slug: "acme",
        name: "Acme Corp",
      },
      role: "owner",
      permissions: ["read", "runs:create", "members:read", "members:write"],
      auth_mode: "cloudflare_access",
    };

    const mockMembers: MemberRecord[] = [
      {
        id: "user-owner",
        subject: "access|owner",
        email: "owner@acme.com",
        display_name: "Owner User",
        role: "owner",
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "user-member",
        subject: "access|member",
        email: "member@acme.com",
        display_name: "Member User",
        role: "member",
        created_at: "2026-01-02T00:00:00Z",
      },
    ];

    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);
    vi.spyOn(api, "getMembers").mockResolvedValue({ members: mockMembers });
    vi.spyOn(api, "getInvitations").mockResolvedValue({
      invitations: [
        {
          organization_id: "org-1",
          email: "pending@acme.com",
          role: "member",
          created_at: "2026-01-03T00:00:00Z",
        },
      ],
    });
    vi.spyOn(api, "getPrompts").mockResolvedValue({ templates: [], last_used: {}, store_path: "prompts.json", exists: true });
    vi.spyOn(api, "getCreators").mockResolvedValue({ creators: [], store_path: "creators.json", exists: true });
    const inviteSpy = vi.spyOn(api, "createInvitation").mockResolvedValue({
      ok: true,
      invitation: {
        organization_id: "org-1",
        email: "new@acme.com",
        role: "member",
        created_at: new Date().toISOString(),
      },
    });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Settings />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText("Organization Members")).toBeInTheDocument();
    expect(await screen.findByText("Owner User")).toBeInTheDocument();
    expect(await screen.findByText("Member User")).toBeInTheDocument();
    expect(await screen.findByText("pending@acme.com")).toBeInTheDocument();

    const inviteBtn = screen.getByRole("button", { name: /Invite Member/i });
    fireEvent.click(inviteBtn);

    const emailInput = screen.getByPlaceholderText(/user@example.com/i);
    fireEvent.change(emailInput, { target: { value: "new@acme.com" } });

    const sendBtn = screen.getByRole("button", { name: /Send Invitation/i });
    fireEvent.click(sendBtn);

    await waitFor(() => {
      expect(inviteSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          email: "new@acme.com",
          role: "member",
        })
      );
    });
  });

  it("renders empty state when organization has no members returned", async () => {
    const mockSession: UserSession = {
      id: "user-admin",
      subject: "access|admin",
      email: "admin@acme.com",
      display_name: "Admin User",
      organization: { id: "org-1", slug: "acme", name: "Acme Corp" },
      role: "admin",
      permissions: ["read", "members:read", "members:write"],
      auth_mode: "cloudflare_access",
    };

    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);
    vi.spyOn(api, "getMembers").mockResolvedValue({ members: [] });
    vi.spyOn(api, "getInvitations").mockResolvedValue({ invitations: [] });
    vi.spyOn(api, "getPrompts").mockResolvedValue({ templates: [], last_used: {}, store_path: "prompts.json", exists: true });
    vi.spyOn(api, "getCreators").mockResolvedValue({ creators: [], store_path: "creators.json", exists: true });

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Settings />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText("No members found in this organization.")).toBeInTheDocument();
  });

  it("renders error state when member fetch fails", async () => {
    const mockSession: UserSession = {
      id: "user-admin",
      subject: "access|admin",
      email: "admin@acme.com",
      display_name: "Admin User",
      organization: { id: "org-1", slug: "acme", name: "Acme Corp" },
      role: "admin",
      permissions: ["read", "members:read", "members:write"],
      auth_mode: "cloudflare_access",
    };

    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);
    vi.spyOn(api, "getMembers").mockRejectedValue(new Error("Network connection failed"));
    vi.spyOn(api, "getInvitations").mockResolvedValue({ invitations: [] });
    vi.spyOn(api, "getPrompts").mockResolvedValue({ templates: [], last_used: {}, store_path: "prompts.json", exists: true });
    vi.spyOn(api, "getCreators").mockResolvedValue({ creators: [], store_path: "creators.json", exists: true });

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Settings />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(
      await screen.findByText(/Failed to load organization members: Network connection failed/i)
    ).toBeInTheDocument();
  });

  it("displays restricted access message when user lacks members:read permission", async () => {
    const mockSession: UserSession = {
      id: "user-viewer",
      subject: "access|viewer",
      email: "viewer@acme.com",
      display_name: "Viewer User",
      organization: {
        id: "org-1",
        slug: "acme",
        name: "Acme Corp",
      },
      role: "viewer",
      permissions: ["read"],
      auth_mode: "cloudflare_access",
    };

    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);
    vi.spyOn(api, "getPrompts").mockResolvedValue({ templates: [], last_used: {}, store_path: "prompts.json", exists: true });
    vi.spyOn(api, "getCreators").mockResolvedValue({ creators: [], store_path: "creators.json", exists: true });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Settings />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(
      await screen.findByText(/Member management is restricted to organization Admins and Owners/i)
    ).toBeInTheDocument();
  });
});
