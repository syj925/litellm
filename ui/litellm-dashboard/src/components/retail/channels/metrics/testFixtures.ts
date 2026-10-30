import type { ChannelDetailMetricsResponse, ChannelMetricsResponse, ChannelPeriodMetrics } from "./types";

export const periodFixture: ChannelPeriodMetrics = {
  requests: 8,
  successful_requests: 6,
  failed_requests: 2,
  unknown_status_requests: 0,
  input_tokens: 120,
  output_tokens: 60,
  total_tokens: 180,
  reasoning_tokens: null,
  cache_read_tokens: 40,
  cache_write_tokens: 8,
  cache_hit_requests: 2,
  cache_status_samples: 8,
  cache_hit_rate_pct: 25,
  streaming_requests: 4,
  usage_samples: 8,
  spend_samples: 8,
  billed_attempts: 1,
  latency_samples: 8,
  ttft_samples: 0,
  native_spend_usd: "0.012345",
  error_rate_pct: 25,
  latency_avg_ms: 350,
  latency_p50_ms: 300,
  latency_p95_ms: 900,
  ttft_avg_ms: null,
  ttft_p50_ms: null,
  ttft_p95_ms: null,
};

const identity = {
  id: "channel-fixture",
  name: "Primary fixture channel",
  pool_id: "fixture-pool",
  pool_name: "Fixture resource",
  status: "active" as const,
  deployment_count: 2,
};
const rolling = { window_seconds: 60 as const, requests: 2, rpm: 2, tpm: 42, native_usd_per_minute: "0.001" };
const window = {
  hours: 1 as const,
  start: "2026-01-01T00:00:00Z",
  end: "2026-01-01T01:00:00Z",
  bucket_minutes: 5,
  rolling_seconds: 60 as const,
};
const coverage = {
  scope: "gateway" as const,
  window_log_rows: 10,
  attributed_requests: 8,
  unattributed_requests: 2,
  model_id_coverage: 0.8,
};

export const channelSummaryFixture: ChannelMetricsResponse = {
  window,
  channels: [{ ...identity, metrics: periodFixture, rolling, upstream_error_events: 3 }],
  coverage,
  notes: ["Fixture attribution note."],
};

export const channelDetailFixture: ChannelDetailMetricsResponse = {
  window,
  channel: identity,
  metrics: periodFixture,
  rolling,
  upstream_error_events: 3,
  models: [{ model: "public-model-fixture", metrics: periodFixture, rolling }],
  trend: [
    { time: "2026-01-01T00:00:00Z", metrics: periodFixture },
    { time: "2026-01-01T00:10:00Z", metrics: { ...periodFixture, requests: 2, native_spend_usd: null } },
  ],
  request_error_groups: [{ error_type: "rate_limit", status_code: 429, count: 2 }],
  upstream_error_groups: [{ error_type: "timeout", status_code: null, count: 3 }],
  recent_errors: [
    {
      time: "2026-01-01T00:30:00Z",
      source: "request",
      request_id: "real-request-fixture",
      model: "public-model-fixture",
      error_type: "rate_limit",
      status_code: 429,
      message: "Request rate limited.",
    },
    {
      time: "2026-01-01T00:29:00Z",
      source: "upstream_event",
      request_id: "upstream-event-fixture",
      model: "public-model-fixture",
      error_type: "timeout",
      status_code: null,
      message: "Upstream timeout event.",
    },
  ],
  coverage,
  notes: ["Fixture attribution note."],
};
