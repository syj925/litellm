import { describe, expect, it } from "vitest";
import { channelTrendPoints } from "./trend";
import { channelDetailFixture } from "./testFixtures";

describe("channel trend alignment", () => {
  it("preserves elapsed time with complete UTC buckets and gaps for unavailable measurements", () => {
    const points = channelTrendPoints(channelDetailFixture.trend, channelDetailFixture.window);
    expect(points).toHaveLength(12);
    expect(points[0].time).toBe("2026-01-01T00:00:00.000Z");
    expect(points[1].time).toBe("2026-01-01T00:05:00.000Z");
    expect(points[2].time).toBe("2026-01-01T00:10:00.000Z");
    expect(points[1].Requests).toBe(0);
    expect(points[1].Failed).toBe(0);
    expect(points[1]["Gateway USD"]).toBeNull();
    expect(points[1]["Latency p50"]).toBeNull();
    expect(points[1]["TTFT p50"]).toBeNull();
    expect(points[1].Input).toBeNull();
    expect(points[0].Requests).toBe(channelDetailFixture.trend[0].metrics.requests);
    expect(new Set(points.map((point) => point.label)).size).toBe(points.length);
  });

  it("aligns partial boundaries to the same UTC grid as stored buckets", () => {
    const window = { ...channelDetailFixture.window, start: "2026-01-01T00:03:00Z", end: "2026-01-01T00:11:00Z" };
    const points = channelTrendPoints(channelDetailFixture.trend, window);
    expect(points.map((point) => point.time)).toEqual([
      "2026-01-01T00:00:00.000Z",
      "2026-01-01T00:05:00.000Z",
      "2026-01-01T00:10:00.000Z",
    ]);
    expect(points[2]["Gateway USD"]).toBeNull();
  });
});
