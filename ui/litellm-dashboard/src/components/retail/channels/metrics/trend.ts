import type { ChannelMetricTrend, ChannelMetricWindow } from "./types";

export interface ChannelTrendPoint extends Record<string, string | number | null> {
  time: string;
  label: string;
  Requests: number;
  Failed: number;
  "Gateway USD": number | null;
  Input: number | null;
  Output: number | null;
  "Cache read": number | null;
  "Latency p50": number | null;
  "Latency p95": number | null;
  "TTFT p50": number | null;
  "TTFT p95": number | null;
}

function spendValue(value: string | null | undefined): number | null {
  if (value == null) return null;
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : null;
}

function bucketLabel(time: number): string {
  const options: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZoneName: "shortOffset",
  };
  return new Intl.DateTimeFormat("en-US", options).format(time);
}

export function channelTrendPoints(trend: ChannelMetricTrend[], window: ChannelMetricWindow): ChannelTrendPoint[] {
  const start = Date.parse(window.start);
  const end = Date.parse(window.end);
  const step = window.bucket_minutes * 60_000;
  const validWindow = Number.isFinite(start) && Number.isFinite(end) && end > start;
  const validStep = Number.isFinite(step) && step > 0;
  if (!validWindow || !validStep) return [];
  const first = Math.floor(start / step) * step;
  const byTime = new Map(
    trend
      .filter((bucket) => Number.isFinite(Date.parse(bucket.time)))
      .map((bucket) => [Math.floor(Date.parse(bucket.time) / step) * step, bucket.metrics] as const),
  );
  return Array.from({ length: Math.ceil((end - first) / step) }, (_, index) => {
    const time = first + index * step;
    const metrics = byTime.get(time);
    return {
      time: new Date(time).toISOString(),
      label: bucketLabel(time),
      Requests: metrics?.requests ?? 0,
      Failed: metrics?.failed_requests ?? 0,
      "Gateway USD": spendValue(metrics?.native_spend_usd),
      Input: metrics?.input_tokens ?? null,
      Output: metrics?.output_tokens ?? null,
      "Cache read": metrics?.cache_read_tokens ?? null,
      "Latency p50": metrics?.latency_p50_ms ?? null,
      "Latency p95": metrics?.latency_p95_ms ?? null,
      "TTFT p50": metrics?.ttft_p50_ms ?? null,
      "TTFT p95": metrics?.ttft_p95_ms ?? null,
    };
  });
}
