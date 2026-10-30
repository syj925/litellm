import type { ChannelHours } from "./route";

export interface ChannelMetricWindow {
  hours: ChannelHours;
  start: string;
  end: string;
  bucket_minutes: number;
  rolling_seconds: 60;
}

export interface ChannelPeriodMetrics {
  requests: number;
  successful_requests: number;
  failed_requests: number;
  unknown_status_requests: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  reasoning_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  cache_write_ttl_tokens?: Record<string, number>;
  cache_write_unclassified_tokens?: number | null;
  cache_hit_requests: number;
  cache_status_samples: number;
  cache_hit_rate_pct: number | null;
  streaming_requests: number;
  usage_samples: number;
  spend_samples: number;
  billed_attempts: number;
  latency_samples: number;
  ttft_samples: number;
  native_spend_usd: string | null;
  error_rate_pct: number | null;
  latency_avg_ms: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  ttft_avg_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
}

export interface ChannelRollingMetrics {
  window_seconds: 60;
  requests: number;
  rpm: number | null;
  tpm: number | null;
  native_usd_per_minute: string | null;
}

export interface ChannelMetricIdentity {
  id: string;
  name: string;
  pool_id: string;
  pool_name: string;
  status: "active" | "provisioning" | "error";
  deployment_count: number;
}

export interface ChannelMetricItem extends ChannelMetricIdentity {
  metrics: ChannelPeriodMetrics;
  rolling: ChannelRollingMetrics;
  upstream_error_events: number;
}

export interface ChannelMetricCoverage {
  scope: "gateway";
  window_log_rows: number;
  attributed_requests: number;
  unattributed_requests: number;
  model_id_coverage: number | null;
}

export interface ChannelMetricsResponse {
  window: ChannelMetricWindow;
  channels: ChannelMetricItem[];
  coverage: ChannelMetricCoverage;
  notes: string[];
}

export interface ChannelModelMetric {
  model: string;
  metrics: ChannelPeriodMetrics;
  rolling: ChannelRollingMetrics;
}

export interface ChannelMetricTrend {
  time: string;
  metrics: ChannelPeriodMetrics;
}

export interface ChannelErrorGroup {
  error_type: string;
  status_code: number | null;
  count: number;
}

export interface ChannelRecentError {
  time: string;
  source: "request" | "upstream_event";
  request_id: string;
  model: string;
  error_type: string;
  status_code: number | null;
  message: string;
}

export interface ChannelDetailMetricsResponse {
  window: ChannelMetricWindow;
  channel: ChannelMetricIdentity;
  metrics: ChannelPeriodMetrics;
  rolling: ChannelRollingMetrics;
  upstream_error_events: number;
  models: ChannelModelMetric[];
  trend: ChannelMetricTrend[];
  request_error_groups: ChannelErrorGroup[];
  upstream_error_groups: ChannelErrorGroup[];
  recent_errors: ChannelRecentError[];
  coverage: ChannelMetricCoverage;
  notes: string[];
}
