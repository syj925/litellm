"use client";

import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, RefreshCw } from "lucide-react";
import { Panel } from "@/app/(dashboard)/usage/_components/components/overview/Primitives";
import { LineChart } from "@/components/shared/charts";
import { Button } from "@/components/ui/button";
import { retailRequest } from "../api";
import type { ModelPerformanceResponse } from "./types";
import {
  modelPerformancePoints,
  performanceCount,
  performanceDuration,
  performancePercent,
  performanceSpeed,
  performanceTime,
} from "./performanceData";

const ranges = [
  { label: "24 hours", hours: 24 },
  { label: "7 days", hours: 168 },
  { label: "30 days", hours: 720 },
] as const;

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="min-w-0 p-4">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-2 text-xl font-semibold tabular-nums">{value}</dd>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</p>
    </div>
  );
}

function ChartState({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-60 items-center justify-center px-5 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function PerformanceCharts({ result }: { result: ModelPerformanceResponse }) {
  const data = modelPerformancePoints(result.trend);
  const buckets = `${result.window.bucket_minutes} minute buckets · UTC`;
  return (
    <div className="grid min-w-0 gap-5 lg:grid-cols-2">
      <Panel title="Request traffic" subtitle={buckets}>
        {result.summary.requests > 0 ? (
          <LineChart
            data={data}
            index="label"
            categories={["Requests", "Failed"]}
            colors={["blue", "red"]}
            connectNulls={false}
            showDots={data.length < 50}
            valueFormatter={performanceCount}
            className="h-60"
          />
        ) : (
          <ChartState>No completed request logs in this time window.</ChartState>
        )}
      </Panel>
      <Panel title="Success rate" subtitle={`${buckets} · final request outcomes`}>
        {data.some((point) => point["Success rate"] != null) ? (
          <LineChart
            data={data}
            index="label"
            categories={["Success rate"]}
            yAxisDomain={[0, 100]}
            colors={["green"]}
            connectNulls={false}
            showDots={data.filter((point) => point["Success rate"] != null).length < 50}
            valueFormatter={performancePercent}
            className="h-60"
          />
        ) : (
          <ChartState>No requests with fully recorded outcomes in this time window.</ChartState>
        )}
      </Panel>
      <Panel title="Time to first token" subtitle={`${buckets} · successful streaming requests`}>
        {data.some((point) => point["TTFT median"] != null || point["TTFT p95"] != null) ? (
          <LineChart
            data={data}
            index="label"
            categories={["TTFT median", "TTFT p95"]}
            colors={["blue", "cyan"]}
            connectNulls={false}
            showDots={data.filter((point) => point["TTFT median"] != null || point["TTFT p95"] != null).length < 50}
            valueFormatter={performanceDuration}
            className="h-60"
          />
        ) : (
          <ChartState>No valid streaming first-token samples. Non-streaming requests do not establish TTFT.</ChartState>
        )}
      </Panel>
      <Panel title="Observed output speed" subtitle={`${buckets} · gateway stream delivery`}>
        {data.some((point) => point["Output speed median"] != null) ? (
          <LineChart
            data={data}
            index="label"
            categories={["Output speed median"]}
            colors={["blue"]}
            connectNulls={false}
            showDots={data.filter((point) => point["Output speed median"] != null).length < 50}
            valueFormatter={performanceSpeed}
            className="h-60"
          />
        ) : (
          <ChartState>No valid output-speed samples. Missing timing data is not estimated.</ChartState>
        )}
      </Panel>
    </div>
  );
}

export default function ModelPerformance({
  model,
  publicView,
  accessToken,
}: {
  model: string;
  publicView: boolean;
  accessToken?: string | null;
}) {
  const [hours, setHours] = useState<number>(24);
  const queryOptions = {
    queryKey: ["retail", "model-performance", publicView ? "public" : accessToken, model, hours],
    queryFn: () =>
      retailRequest<ModelPerformanceResponse>(
        `${publicView ? "/public/model-metrics" : "/model-metrics"}?${new URLSearchParams({ model, hours: String(hours) })}`,
        publicView ? null : accessToken ?? null,
      ),
    enabled: publicView || Boolean(accessToken),
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: false,
  };
  const metrics = useQuery(queryOptions);
  const result = metrics.data;
  const summary = result?.summary;
  return (
    <section aria-label="Model performance" className="min-w-0 space-y-5 pt-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="flex items-center gap-2 font-semibold">
            <Activity className="size-4" /> Site performance
          </h2>
          <p className="mt-1 max-w-2xl text-xs leading-6 text-muted-foreground">
            {publicView
              ? "Aggregated observations from this site's published public resources."
              : "Observations from your account's requests only."}{" "}
            Completed request logs can arrive late. These are observations, not capacity limits or uptime guarantees.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Performance time range">
          {ranges.map((range) => (
            <Button
              key={range.hours}
              variant={hours === range.hours ? "secondary" : "ghost"}
              size="sm"
              aria-pressed={hours === range.hours}
              onClick={() => setHours(range.hours)}
            >
              {range.label}
            </Button>
          ))}
          <Button
            variant="outline"
            size="icon-sm"
            aria-label="Refresh model performance"
            disabled={metrics.isFetching}
            onClick={() => metrics.refetch()}
          >
            <RefreshCw className={metrics.isFetching ? "animate-spin" : ""} />
          </Button>
        </div>
      </div>
      {metrics.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading site observations...
        </p>
      )}
      {metrics.isError && (
        <p role="alert" className="rounded-lg border p-4 text-sm text-muted-foreground">
          Performance data is unavailable. Refresh to try again.
        </p>
      )}
      {result && summary && (
        <>
          <dl className="grid divide-y rounded-lg border sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4 lg:divide-x">
            <Metric
              label="Requests"
              value={performanceCount(summary.requests)}
              detail={`${performanceCount(summary.successful_requests)} succeeded · ${performanceCount(summary.failed_requests)} failed`}
            />
            <Metric
              label="Success rate"
              value={performancePercent(summary.success_rate_pct)}
              detail={
                summary.unknown_status_requests
                  ? `${performanceCount(summary.unknown_status_requests)} outcomes unknown`
                  : "Final recorded request outcomes"
              }
            />
            <Metric
              label="Median TTFT"
              value={summary.ttft_samples > 0 ? performanceDuration(summary.ttft_p50_ms) : "Not available"}
              detail={`${performanceCount(summary.ttft_samples)} measured streaming samples`}
            />
            <Metric
              label="Median output speed"
              value={
                summary.throughput_samples > 0
                  ? performanceSpeed(summary.output_tokens_per_second_p50)
                  : "Not available"
              }
              detail={`${performanceCount(summary.throughput_samples)} measured streaming samples`}
            />
          </dl>
          <p className="text-xs leading-6 text-muted-foreground">
            {performanceTime(result.window.start)} to {performanceTime(result.window.end)} UTC · Input tokens:{" "}
            {performanceCount(summary.input_tokens)} · Output tokens: {performanceCount(summary.output_tokens)} · Empty
            periods have no measured latency or speed.
          </p>
          <PerformanceCharts result={result} />
          {result.notes?.length ? (
            <details className="rounded-lg border px-4 py-3 text-xs text-muted-foreground">
              <summary className="cursor-pointer font-medium">Measurement details</summary>
              <div className="mt-3 space-y-2 leading-6">
                {result.notes.map((note) => (
                  <p key={note}>{note}</p>
                ))}
              </div>
            </details>
          ) : null}
        </>
      )}
    </section>
  );
}
