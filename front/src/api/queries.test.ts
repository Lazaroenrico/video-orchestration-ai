import { describe, expect, it } from "vitest";
import { campaignRowsFromIndex } from "./queries";

describe("campaignRowsFromIndex", () => {
  it("keeps cancelled runs separate from failures", () => {
    const rows = campaignRowsFromIndex(
      {
        runs: ["web-cancelled", "web-failed"],
        active: [],
        errored: ["web-failed"],
        cancelled: ["web-cancelled"],
      },
      {}
    );

    expect(rows).toEqual([
      {
        id: "web-cancelled",
        active: false,
        errored: false,
        cancelled: true,
        summary: null,
      },
      {
        id: "web-failed",
        active: false,
        errored: true,
        cancelled: false,
        summary: null,
      },
    ]);
  });
});
