import { describe, expect, it } from "vitest";
import { QueryClient, dehydrate } from "@tanstack/react-query";
import { shouldPersistQuery, QUERY_CACHE_BUSTER } from "./queryClient";

describe("queryClient persistence filter", () => {
  it("uses cache buster v2 or higher to invalidate stale legacy caches", () => {
    expect(QUERY_CACHE_BUSTER).not.toBe("orchestrator-front-cache-v1");
  });

  it("excludes session, members, and invitations from persistence", () => {
    const fakeSessionQuery = {
      queryKey: ["session", "me"],
      state: { status: "success", data: { id: "user-1", role: "owner" } },
    } as any;

    const fakeMembersQuery = {
      queryKey: ["members"],
      state: { status: "success", data: { members: [] } },
    } as any;

    const fakeInvitationsQuery = {
      queryKey: ["invitations"],
      state: { status: "success", data: { invitations: [] } },
    } as any;

    const fakeCreatorsQuery = {
      queryKey: ["creators"],
      state: { status: "success", data: { creators: [] } },
    } as any;

    expect(shouldPersistQuery(fakeSessionQuery)).toBe(false);
    expect(shouldPersistQuery(fakeMembersQuery)).toBe(false);
    expect(shouldPersistQuery(fakeInvitationsQuery)).toBe(false);
    expect(shouldPersistQuery(fakeCreatorsQuery)).toBe(true);
  });

  it("dehydrate excludes sensitive auth, member and invitation queries", () => {
    const client = new QueryClient();

    client.setQueryData(["session", "me"], { id: "user-1", role: "owner" });
    client.setQueryData(["members"], { members: [{ id: "m-1" }] });
    client.setQueryData(["invitations"], { invitations: [{ email: "a@b.com" }] });
    client.setQueryData(["creators"], { creators: [{ id: "c-1" }] });

    const dehydrated = dehydrate(client, {
      shouldDehydrateQuery: shouldPersistQuery,
    });

    const dehydratedKeys = dehydrated.queries.map((q) => q.queryKey);
    expect(dehydratedKeys).toContainEqual(["creators"]);
    expect(dehydratedKeys).not.toContainEqual(["session", "me"]);
    expect(dehydratedKeys).not.toContainEqual(["members"]);
    expect(dehydratedKeys).not.toContainEqual(["invitations"]);
  });
});
