import type { ModelPerformanceResponse } from "./types";

const countFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const timeOptions: Intl.DateTimeFormatOptions = {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
};
const timeFormat = new Intl.DateTimeFormat("en-US", timeOptions);

export function performanceCount(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "Not available" : countFormat.format(value);
}

export function performanceDuration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "Not available";
  return value >= 1000 ? `${countFormat.format(value / 1000)} s` : `${countFormat.format(value)} ms`;
}

export function performancePercent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "Not available" : `${countFormat.format(value)}%`;
}

export function performanceSpeed(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) || value < 0 ? "Not available" : `${countFormat.format(value)} tok/s`;
}

export function performanceTime(value: string): string {
  const time = Date.parse(value);
  return Number.isFinite(time) ? timeFormat.format(time) : "Unknown time";
}

export interface PerformancePoint extends Record<string, string | number | null> {
  label: string;
  Requests: number;
  Failed: number;
  "Success rate": number | null;
  "TTFT median": number | null;
  "TTFT p95": number | null;
  "Output speed median": number | null;
}

export function modelPerformancePoints(trend: ModelPerformanceResponse["trend"]): PerformancePoint[] {
  return trend
    .filter((point) => Number.isFinite(Date.parse(point.time)))
    .map((point) => ({
      label: performanceTime(point.time),
      Requests: point.requests,
      Failed: point.failed_requests,
      "Success rate": point.success_rate_pct,
      "TTFT median": point.ttft_samples > 0 ? point.ttft_p50_ms : null,
      "TTFT p95": point.ttft_samples > 0 ? point.ttft_p95_ms : null,
      "Output speed median": point.throughput_samples > 0 ? point.output_tokens_per_second_p50 : null,
    }));
}
