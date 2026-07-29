import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, HttpError } from "./client";
import {
  queryKeys,
  useReviewRunV2Mutation,
} from "./queries";

describe("useReviewRunV2Mutation", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("invalidates the run after a conflict so stale review UI is refreshed", async () => {
    vi.spyOn(api, "reviewRunV2").mockRejectedValue(
      new HttpError(
        409,
        "gate não corresponde à revisão pendente deste run",
      ),
    );
    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useReviewRunV2Mutation(), { wrapper });

    await act(async () => {
      await expect(
        result.current.mutateAsync({
          runId: "web-test",
          action: "approve",
          gate: {
            gate_id: "gate-1",
            version: 1,
            gate_type: "review_creative_plan",
          },
        }),
      ).rejects.toBeInstanceOf(HttpError);
    });

    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: queryKeys.runState("web-test"),
      });
    });
  });
});
