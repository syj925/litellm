import { describe, expect, it } from "vitest";
import { channelHoursParser, channelDetailParsers, channelDetailHref } from "./route";
import { formatChannelCount, formatChannelDuration, formatChannelPercent, formatChannelUsd } from "./format";

describe("channel metric values", () => {
  it("keeps unavailable measurements distinct from measured zero", () => {
    expect(formatChannelCount(null)).toBe("—");
    expect(formatChannelCount(0)).toBe("0");
    expect(formatChannelDuration(undefined)).toBe("—");
    expect(formatChannelDuration(0)).toBe("0 ms");
    expect(formatChannelUsd(null)).toBe("—");
    expect(formatChannelUsd("0")).toBe("$0.00");
    expect(formatChannelPercent(null)).toBe("—");
    expect(formatChannelPercent(0)).toBe("0%");
  });

  it("does not round tiny positive provider spend into zero", () => {
    expect(formatChannelUsd("0.000000001")).toBe("<$0.000001");
    expect(formatChannelUsd("0.012345")).toBe("$0.012345");
  });

  it("validates dashboard time windows and tab state before consuming URL values", () => {
    expect(channelHoursParser.parse("3")).toBeNull();
    expect(channelHoursParser.parse("168")).toBe(168);
    expect(channelDetailParsers.tab.parse("operator-secret")).toBeNull();
    expect(channelDetailParsers.tab.parse("errors")).toBe("errors");
    expect(channelDetailHref("id/with space", 6)).toBe("/ui/channel-details?channel=id%2Fwith%20space&hours=6");
  });
});
