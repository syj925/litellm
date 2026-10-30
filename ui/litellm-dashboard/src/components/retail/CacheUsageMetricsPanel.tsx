import { useQuery } from "@tanstack/react-query";
import { apiClient, formatDate } from "@/components/networking";
import CacheUsageBreakdown from "./CacheUsageBreakdown";
import type { CacheUsageSummary } from "./cacheUsage";

interface CacheUsageMetrics extends CacheUsageSummary {
  request_count: number;
  cache_usage_samples: number;
  cache_usage_unknown_requests: number;
}

interface CacheUsageMetricsPanelProps {
  accessToken: string | null;
  startTime: Date | null;
  endTime: Date | null;
  scope: "global" | "self";
  userId: string | null;
  modelKeys?: readonly string[];
  modelDimension?: "models" | "model_groups";
  pending?: boolean;
}

function usageDay(date: Date | null, offsetDays = 0): string | null {
  if (!date || !Number.isFinite(date.getTime())) return null;
  return new Date(Date.parse(`${formatDate(date)}T00:00:00.000Z`) + offsetDays * 86_400_000).toISOString();
}

function unavailableFilter(
  props: Pick<CacheUsageMetricsPanelProps, "accessToken" | "scope" | "userId" | "modelKeys">,
  start: string | null,
  end: string | null,
): string | null {
  if (!props.accessToken || !start || !end)
    return "Cache usage is unavailable until the account and date range are ready.";
  if (props.scope === "self" && !props.userId) return "Cache usage is unavailable until the account is ready.";
  const duration = Date.parse(end) - Date.parse(start);
  if (duration <= 0 || duration > 31 * 86_400_000) return "Cache usage is available for date ranges of up to 31 days.";
  if (props.modelKeys && (props.modelKeys.length === 0 || props.modelKeys.length > 500)) {
    return "Cache usage is unavailable for this model filter. Select between 1 and 500 matching models.";
  }
  return null;
}

function CacheMetricsContent({
  status,
  failed,
  data,
}: {
  status: string | null;
  failed: boolean;
  data: CacheUsageMetrics | undefined;
}) {
  if (status || failed) {
    return (
      <p role="status" className="rounded-lg border p-4 text-sm text-muted-foreground">
        {status ?? "Cache usage is unavailable for the current filter."}
      </p>
    );
  }
  if (!data) return null;
  return (
    <>
      <CacheUsageBreakdown usage={data} />
      <p className="text-xs text-muted-foreground">
        Cache counts recorded for {data.cache_usage_samples.toLocaleString()} of {data.request_count.toLocaleString()}{" "}
        final requests. {data.cache_usage_unknown_requests.toLocaleString()} requests have incomplete cache counts.
        Retry helper rows are excluded.
      </p>
    </>
  );
}

export default function CacheUsageMetricsPanel({
  accessToken,
  startTime,
  endTime,
  scope,
  userId,
  modelKeys,
  modelDimension = "model_groups",
  pending = false,
}: CacheUsageMetricsPanelProps) {
  const start = usageDay(startTime);
  const end = usageDay(endTime, 1);
  const filter = { accessToken, scope, userId, modelKeys };
  const unavailable = unavailableFilter(filter, start, end);
  const queryOptions = {
    queryKey: ["retail-cache-usage", accessToken, start, end, scope, userId, modelDimension, modelKeys],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      apiClient.get<CacheUsageMetrics>("/retail/cache-usage", {
        accessToken,
        signal,
        query: {
          start_date: start,
          end_date: end,
          scope,
          user_id: userId,
          ...(modelKeys ? { [modelDimension]: [...modelKeys] } : {}),
        },
      }),
    enabled: !unavailable && !pending,
    throwOnError: false,
    retry: false,
    staleTime: 15_000,
  };
  const metrics = useQuery(queryOptions);
  const status = unavailable ?? (pending || metrics.isPending ? "Loading cache usage for the current filter." : null);
  const dimensionLabel = modelDimension === "models" ? "models" : "model groups";
  return (
    <div className="my-5 space-y-2" aria-label="Cache duration summary">
      <h2 className="text-sm font-semibold">Cache usage by duration</h2>
      <p className="text-xs text-muted-foreground">
        Current date and user filters, using the same UTC day buckets as Usage.
        {modelKeys ? ` Limited to ${modelKeys.length} matching ${dimensionLabel}.` : " All models in this view."}
      </p>
      <CacheMetricsContent status={status} failed={metrics.isError} data={metrics.data} />
    </div>
  );
}
