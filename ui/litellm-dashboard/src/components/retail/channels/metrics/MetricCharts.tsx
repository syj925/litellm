"use client";

import { LineChart } from "@/components/shared/charts";
import { Panel } from "@/app/(dashboard)/usage/_components/components/overview/Primitives";
import { formatChannelCount, formatChannelDuration, formatChannelUsd } from "./format";
import type { ChannelMetricTrend, ChannelMetricWindow } from "./types";
import { channelTrendPoints } from "./trend";

export function ChannelMetricCharts({ trend, window }: { trend: ChannelMetricTrend[]; window: ChannelMetricWindow }) {
  const data = channelTrendPoints(trend, window);
  const subtitle = window.bucket_minutes + " minute buckets · local time with UTC offset";
  return (
    <div className="grid min-w-0 gap-5 lg:grid-cols-2">
      <Panel title="Request traffic" subtitle={subtitle}>
        {data.length ? (
          <LineChart
            data={data}
            index="label"
            connectNulls={false}
            categories={["Requests", "Failed"]}
            colors={["blue", "red"]}
            valueFormatter={formatChannelCount}
            className="h-64"
          />
        ) : (
          <ChartEmpty />
        )}
      </Panel>
      <Panel title="Native gateway spend" subtitle={subtitle + " · recorded USD"}>
        {data.some((item) => item["Gateway USD"] != null) ? (
          <LineChart
            data={data}
            index="label"
            connectNulls={false}
            categories={["Gateway USD"]}
            showDots={trend.length < 50}
            colors={["blue"]}
            valueFormatter={formatChannelUsd}
            className="h-64"
          />
        ) : (
          <ChartEmpty description="No native spend samples recorded." />
        )}
      </Panel>
      <Panel title="Token usage" subtitle={subtitle + " · billed attempts included"}>
        {data.some((item) => item.Input != null || item.Output != null) ? (
          <LineChart
            data={data}
            index="label"
            connectNulls={false}
            categories={["Input", "Output", "Cache read"]}
            showDots={trend.length < 50}
            colors={["blue", "green", "cyan"]}
            valueFormatter={formatChannelCount}
            className="h-64"
          />
        ) : (
          <ChartEmpty description="No token usage samples recorded." />
        )}
      </Panel>
      <Panel title="Latency and first token" subtitle={subtitle + " · measured samples only"}>
        {data.some((item) => item["Latency p50"] != null || item["TTFT p50"] != null) ? (
          <LineChart
            data={data}
            index="label"
            connectNulls={false}
            categories={["Latency p50", "Latency p95", "TTFT p50", "TTFT p95"]}
            showDots={trend.length < 50}
            colors={["blue", "cyan", "green", "red"]}
            valueFormatter={formatChannelDuration}
            className="h-64"
          />
        ) : (
          <ChartEmpty description="No latency or streaming TTFT samples recorded." />
        )}
      </Panel>
    </div>
  );
}

function ChartEmpty({ description = "No attributed request logs in this time window." }: { description?: string }) {
  return (
    <div className="flex h-64 items-center justify-center px-6 text-center text-sm text-muted-foreground">
      {description}
    </div>
  );
}
