"use client";

import { useMemo, useState } from "react";
import { useQueryState } from "nuqs";
import { Activity, ShieldCheck } from "lucide-react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { DataTable, DataTableToolbar } from "@/components/shared/DataTable";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { isProxyAdminRole } from "@/utils/roles";
import { parseAsString } from "nuqs";
import { MetricControls, MetricCoverage, MetricFreshness } from "./MetricControls";
import { MetricStat } from "./MetricStats";
import { channelSummaryColumns } from "./MetricTables";
import { formatChannelCount } from "./format";
import { channelHoursParser, type ChannelHours } from "./route";
import { useChannelMetrics } from "./queries";

export interface ChannelActivityPanelProps {
  hours?: ChannelHours;
  accessToken?: string | null;
}

export function ChannelActivityPanel({ hours, accessToken }: ChannelActivityPanelProps) {
  const auth = useAuthorized();
  const admin = isProxyAdminRole(auth.userRole);
  const [windowHours, setWindowHours] = useQueryState("hours", channelHoursParser.withDefault(hours ?? 24));
  const [search, setSearch] = useQueryState("channel_search", parseAsString.withDefault(""));
  const [paused, setPaused] = useState(false);
  const queryOptions = {
    accessToken: accessToken ?? auth.accessToken,
    hours: windowHours,
    enabled: auth.isAuthorized && admin,
    paused,
  };
  const metrics = useChannelMetrics(queryOptions);
  const columns = useMemo(() => channelSummaryColumns(windowHours), [windowHours]);
  const needle = search.trim().toLowerCase();
  const channels = metrics.data?.channels ?? [];
  const filtered = channels.filter((channel) =>
    (channel.name + " " + channel.pool_name + " " + channel.id).toLowerCase().includes(needle),
  );
  if (auth.isLoading || !auth.isAuthorized)
    return <LoadingState title="Loading channel analytics" description="Verifying administrator access." />;
  if (!admin)
    return (
      <StateMessage
        role="alert"
        icon={<ShieldCheck />}
        title="Administrator access required"
        description="Channel operations metrics are available to proxy administrators."
      />
    );
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Activity className="size-5" /> Channel analytics
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Traffic, recorded spend and performance by upstream channel.
          </p>
        </div>
        <MetricControls
          hours={windowHours}
          onHoursChange={(value) => void setWindowHours(value)}
          paused={paused}
          onPausedChange={setPaused}
          isFetching={metrics.isFetching}
          onRefresh={() => void metrics.refetch()}
        />
      </div>
      <MetricFreshness
        paused={paused}
        updatedAt={metrics.dataUpdatedAt}
        error={metrics.error}
        hasData={Boolean(metrics.data)}
      />
      {metrics.isPending && (
        <LoadingState
          title="Loading channel analytics"
          description="Reading attributed request logs and recent upstream errors."
        />
      )}
      {metrics.data && (
        <>
          <dl className="grid divide-y rounded-xl border sm:grid-cols-2 xl:grid-cols-4 xl:divide-x xl:divide-y-0">
            <MetricStat
              label="Channels"
              value={formatChannelCount(channels.length)}
              description="Channels in the selected snapshot"
            />
            <MetricStat
              label="Recorded requests"
              value={formatChannelCount(channels.reduce((total, item) => total + item.metrics.requests, 0))}
              description="Attributed terminal requests in the selected time window"
            />
            <MetricStat
              label="Failed requests"
              value={formatChannelCount(channels.reduce((total, item) => total + item.metrics.failed_requests, 0))}
              description="Unknown statuses are reported separately"
            />
            <MetricStat
              label="Requests started in last 60s"
              value={formatChannelCount(channels.reduce((total, item) => total + item.rolling.requests, 0))}
              description="Completed and persisted logs only"
            />
          </dl>
          <DataTable
            data={filtered}
            columns={columns}
            getRowId={(row) => row.id}
            sortingMode="client"
            defaultSorting={[{ id: "requests", desc: true }]}
            paginationMode="client"
            size="compact"
            noDataMessage={needle ? "No channels match your search." : "No named channels are configured."}
            defaultColumnVisibility={{
              latency_p95_ms: false,
              ttft_p95_ms: false,
              cache_read_tokens: false,
              cache_write_tokens: false,
            }}
            toolbar={(table) => (
              <DataTableToolbar
                table={table}
                searchValue={search}
                onSearchChange={(value) => void setSearch(value)}
                searchPlaceholder="Search channel or resource pool"
              />
            )}
          />
          <MetricCoverage coverage={metrics.data.coverage} notes={metrics.data.notes} />
        </>
      )}
    </div>
  );
}

export default ChannelActivityPanel;
