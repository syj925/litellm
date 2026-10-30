import { describe, expect, it } from "vitest";
import {
  modelPerformancePoints,
  performanceCount,
  performanceDuration,
  performancePercent,
  performanceSpeed,
  performanceTime,
} from "./performanceData";
import type { ModelPerformanceMetrics } from "./types";

const metrics: ModelPerformanceMetrics = {
  requests: 0,
  successful_requests: 0,
  failed_requests: 0,
  unknown_status_requests: 0,
  success_rate_pct: null,
  input_tokens: null,
  output_tokens: null,
  total_tokens: null,
  latency_p50_ms: null,
  latency_p95_ms: null,
  ttft_p50_ms: null,
  ttft_p95_ms: null,
  output_tokens_per_second_p50: null,
  ttft_samples: 0,
  throughput_samples: 0,
};

describe("site model performance formatting", () => {
  it("keeps observed zero distinct from absent measurements", () => {
    expect(performanceCount(0)).toBe("0");
    expect(performancePercent(0)).toBe("0%");
    expect(performanceDuration(0)).toBe("0 ms");
    expect(performanceSpeed(0)).toBe("0 tok/s");
    expect(performanceDuration(null)).toBe("Not available");
    expect(performancePercent(undefined)).toBe("Not available");
    expect(performanceSpeed(Number.NaN)).toBe("Not available");
    expect(performanceSpeed(-1)).toBe("Not available");
    expect(performanceDuration(1500)).toBe("1.5 s");
  });

  it("uses UTC labels independent of the reader's timezone", () => {
    expect(performanceTime("2026-01-01T00:15:00+08:00")).toBe(performanceTime("2025-12-31T16:15:00Z"));
    expect(performanceTime("invalid")).toBe("Unknown time");
  });

  it("preserves measurement gaps instead of deriving TTFT or throughput from request latency", () => {
    const points = modelPerformancePoints([
      {
        ...metrics,
        time: "2026-01-01T00:00:00Z",
        requests: 2,
        latency_p50_ms: 900,
        ttft_p50_ms: 400,
        output_tokens_per_second_p50: 30,
      },
      {
        ...metrics,
        time: "2026-01-01T00:15:00Z",
        requests: 1,
        ttft_p50_ms: 500,
        ttft_p95_ms: 600,
        ttft_samples: 1,
        output_tokens_per_second_p50: 25,
        throughput_samples: 1,
      },
    ]);
    expect(points).toHaveLength(2);
    expect(points[0]["TTFT median"]).toBeNull();
    expect(points[0]["Output speed median"]).toBeNull();
    expect(points[0].Requests).toBe(2);
    expect(points[0]["Success rate"]).toBeNull();
    expect(points[1]["TTFT median"]).toBe(500);
    expect(points[1]["Output speed median"]).toBe(25);
  });
});
