import { describe, expect, it } from "vitest";
import type { Item } from "../types";
import { itemStatus as campaignItemStatus } from "./CampaignDetail";
import { itemStatus as reviewItemStatus } from "./VideoReview";

const failedVideo: Item = {
  id: "item-1",
  attempts: 0,
  cost_usd: 0,
  artifacts: [],
  dropped: false,
  error: "video provider operation failed",
  failure: {
    code: "prediction_timeout",
    type: "WriteTimeout",
    message: "video provider operation failed",
    stage: "talking_head",
    provider: "replicate",
    item_id: "item-1",
    effect_key: "video:run-1:item-1:talking_head:0:hash",
    retryable: false,
    uncertain: true,
  },
};

describe("structured item failure labels", () => {
  it("identifies video generation instead of mislabeling it as assembly", () => {
    expect(reviewItemStatus(failedVideo).label).toBe("Video Generation Failed");
    expect(campaignItemStatus(failedVideo).label).toBe("Video Generation Failed");
  });

  it("keeps legacy errors generic when no stage was persisted", () => {
    const legacy = { ...failedVideo, failure: null };
    expect(reviewItemStatus(legacy).label).toBe("Production Failed");
    expect(campaignItemStatus(legacy).label).toBe("Production Failed");
  });
});
