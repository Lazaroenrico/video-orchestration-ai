import { afterEach, describe, expect, it, vi } from "vitest";
import { api, HttpError } from "./client";

describe("api client errors", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("preserves the HTTP status and response detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: "gate não corresponde à revisão pendente deste run",
          }),
          {
            status: 409,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    const request = api.reviewRunV2("web-test", {
      action: "approve",
      gate_id: "gate-1",
      version: 1,
      gate_type: "review_creative_plan",
    });

    await expect(request).rejects.toBeInstanceOf(HttpError);
    await expect(request).rejects.toMatchObject({
      status: 409,
      detail: "gate não corresponde à revisão pendente deste run",
      message: "409 gate não corresponde à revisão pendente deste run",
    });
  });
});
