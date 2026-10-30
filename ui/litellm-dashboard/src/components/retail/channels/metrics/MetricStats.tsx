import type { ReactNode } from "react";
import { formatChannelCount, formatChannelDuration, formatChannelPercent, formatChannelUsd } from "./format";
import type { ChannelPeriodMetrics, ChannelRollingMetrics } from "./types";

export function MetricStat({ label, value, description }: { label: string; value: string; description: ReactNode }) {
  return (
    <div className="min-w-0 p-5">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd aria-label={label} className="mt-2 text-xl font-semibold tracking-tight tabular-nums">
        {value}
      </dd>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>
    </div>
  );
}

export function RollingMetricStrip({ metrics }: { metrics: ChannelRollingMetrics }) {
  return (
    <section aria-label="Rolling 60 second metrics">
      <h2 className="mb-3 text-sm font-semibold">Last 60 seconds</h2>
      <dl className="grid divide-y rounded-xl border md:grid-cols-3 md:divide-x md:divide-y-0">
        <MetricStat
          label="RPM"
          value={formatChannelCount(metrics.rpm)}
          description={formatChannelCount(metrics.requests) + " completed logs from requests started in this window"}
        />
        <MetricStat
          label="TPM"
          value={formatChannelCount(metrics.tpm)}
          description="Recorded input and output tokens per minute"
        />
        <MetricStat
          label="Native gateway USD / min"
          value={formatChannelUsd(metrics.native_usd_per_minute)}
          description="Recorded LiteLLM spend per minute"
        />
      </dl>
    </section>
  );
}

export function PeriodMetricGrid({ metrics }: { metrics: ChannelPeriodMetrics }) {
  return (
    <section aria-label="Time window metrics">
      <h2 className="mb-3 text-sm font-semibold">Selected time window</h2>
      <dl className="grid divide-y rounded-xl border sm:grid-cols-2 xl:grid-cols-4">
        <MetricStat
          label="Total requests"
          value={formatChannelCount(metrics.requests)}
          description={
            formatChannelCount(metrics.successful_requests) +
            " successful · " +
            formatChannelCount(metrics.failed_requests) +
            " failed"
          }
        />
        <MetricStat
          label="Failure rate"
          value={formatChannelPercent(metrics.error_rate_pct)}
          description={formatChannelCount(metrics.unknown_status_requests) + " requests with unknown status"}
        />
        <MetricStat
          label="Native gateway USD"
          value={formatChannelUsd(metrics.native_spend_usd)}
          description={
            formatChannelCount(metrics.spend_samples) +
            " spend samples · " +
            formatChannelCount(metrics.billed_attempts) +
            " billed attempts"
          }
        />
        <MetricStat
          label="Total tokens"
          value={formatChannelCount(metrics.total_tokens)}
          description={
            formatChannelCount(metrics.input_tokens) +
            " input · " +
            formatChannelCount(metrics.output_tokens) +
            " output"
          }
        />
        <MetricStat
          label="Gateway cache hit rate"
          value={formatChannelPercent(metrics.cache_hit_rate_pct)}
          description={
            formatChannelCount(metrics.cache_hit_requests) +
            " hits · " +
            formatChannelCount(metrics.cache_status_samples) +
            " cache-status samples"
          }
        />
        <MetricStat
          label="Cache read / write tokens"
          value={formatChannelCount(metrics.cache_read_tokens) + " / " + formatChannelCount(metrics.cache_write_tokens)}
          description={
            formatChannelCount(metrics.reasoning_tokens) +
            " reasoning tokens · " +
            formatChannelCount(metrics.usage_samples) +
            " usage samples"
          }
        />
        <MetricStat
          label="Latency p50"
          value={formatChannelDuration(metrics.latency_p50_ms)}
          description={
            "Avg " +
            formatChannelDuration(metrics.latency_avg_ms) +
            " · p95 " +
            formatChannelDuration(metrics.latency_p95_ms) +
            " · " +
            formatChannelCount(metrics.latency_samples) +
            " timed requests"
          }
        />
        <MetricStat
          label="TTFT p50"
          value={formatChannelDuration(metrics.ttft_p50_ms)}
          description={
            "Avg " +
            formatChannelDuration(metrics.ttft_avg_ms) +
            " · p95 " +
            formatChannelDuration(metrics.ttft_p95_ms) +
            " · " +
            formatChannelCount(metrics.ttft_samples) +
            " / " +
            formatChannelCount(metrics.streaming_requests) +
            " streamed requests sampled"
          }
        />
      </dl>
      <p className="mt-2 text-xs leading-6 text-muted-foreground">
        — means unavailable. TTFT is shown only for recorded streaming measurements. Usage and spend include recorded
        billed attempts; request counts, failure rates and timing describe terminal requests.
      </p>
    </section>
  );
}
