import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";
import {
  queryKeys,
  useSessionQuery,
  useMembersQuery,
  useGrantMemberMutation,
  useUpdateMemberRoleMutation,
  useRevokeMemberMutation,
} from "./queries";
import type { UserSession, MemberRecord } from "./contracts";

describe("Session & Member queries", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("fetches authenticated session profile", async () => {
    const mockSession: UserSession = {
      id: "00000000-0000-0000-0000-000000000001",
      subject: "access|bob",
      email: "bob@example.com",
      display_name: "Bob Builder",
      organization: {
        id: "00000000-0000-0000-0000-000000000002",
        slug: "acme",
        name: "Acme Inc.",
      },
      role: "admin",
      permissions: ["read", "runs:create", "members:read", "members:write"],
      auth_mode: "cloudflare_access",
    };
    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useSessionQuery(), { wrapper });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
      expect(result.current.data?.subject).toBe("access|bob");
      expect(result.current.data?.role).toBe("admin");
      expect(result.current.data?.permissions).toContain("members:write");
    });
  });

  it("manages members and invalidates member queries on mutations", async () => {
    const mockMember: MemberRecord = {
      id: "user-1",
      subject: "access|alice",
      email: "alice@example.com",
      display_name: "Alice",
      role: "member",
      created_at: new Date().toISOString(),
    };

    vi.spyOn(api, "getMembers").mockResolvedValue({ members: [mockMember] });
    vi.spyOn(api, "grantMember").mockResolvedValue({ ok: true, member: mockMember });
    vi.spyOn(api, "updateMemberRole").mockResolvedValue({
      ok: true,
      member: { ...mockMember, role: "viewer" },
    });
    vi.spyOn(api, "revokeMember").mockResolvedValue({ ok: true });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const membersHook = renderHook(() => useMembersQuery(), { wrapper });
    await waitFor(() => {
      expect(membersHook.result.current.isSuccess).toBe(true);
      expect(membersHook.result.current.data?.members).toHaveLength(1);
      expect(membersHook.result.current.data?.members[0].subject).toBe("access|alice");
    });

    const grantHook = renderHook(() => useGrantMemberMutation(), { wrapper });
    await act(async () => {
      await grantHook.result.current.mutateAsync({
        subject: "access|alice",
        role: "member",
        email: "alice@example.com",
      });
    });

    expect(invalidate).toHaveBeenCalledWith({
      queryKey: queryKeys.members(),
    });

    const updateHook = renderHook(() => useUpdateMemberRoleMutation(), { wrapper });
    await act(async () => {
      await updateHook.result.current.mutateAsync({
        subject: "access|alice",
        role: "viewer",
      });
    });

    expect(invalidate).toHaveBeenCalledWith({
      queryKey: queryKeys.members(),
    });

    const revokeHook = renderHook(() => useRevokeMemberMutation(), { wrapper });
    await act(async () => {
      await revokeHook.result.current.mutateAsync("access|alice");
    });

    expect(invalidate).toHaveBeenCalledWith({
      queryKey: queryKeys.members(),
    });
  });
});
