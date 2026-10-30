"use client";

import { useState } from "react";
import Link from "next/link";
import { useQueryStates } from "nuqs";
import { ArrowLeft, Cable, ShieldCheck } from "lucide-react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import CopyButton from "@/components/shared/CopyButton";
import { StatusBadge } from "@/components/shared/table_cells";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { channelDetailParsers, channelDetailTabs, type ChannelDetailTab } from "./route";
import { useChannelDetailMetrics } from "./queries";
import { MetricControls, MetricCoverage, MetricFreshness } from "./MetricControls";
import { PeriodMetricGrid, RollingMetricStrip } from "./MetricStats";
import { ChannelMetricCharts } from "./MetricCharts";
import { ChannelModelsTable, ErrorGroups, RecentErrors } from "./MetricTables";
import { formatChannelConfigState, formatChannelCount, formatChannelTimestamp } from "./format";

const tabLabels: Readonly<Record<ChannelDetailTab, string>> = {
  overview: "Overview",
  models: "Models",
  errors: "Errors",
};

export default function ChannelDetailsPanel() {
  const auth = useAuthorized();
  const admin = isProxyAdminRole(auth.userRole);
  const [{ channel: channelId, hours, tab }, setRoute] = useQueryStates(channelDetailParsers);
  const [paused, setPaused] = useState(false);
  const queryOptions = {
    accessToken: auth.accessToken,
    hours,
    enabled: auth.isAuthorized && admin,
    paused,
  };
  const metrics = useChannelDetailMetrics(channelId, queryOptions);
  if (auth.isLoading || !auth.isAuthorized)
    return <LoadingState title="Loading channel details" description="Verifying administrator access." />;
  if (!admin)
    return (
      <StateMessage
        role="alert"
        icon={<ShieldCheck />}
        title="Administrator access required"
        description="Channel details are available to proxy administrators."
      />
    );
  if (!channelId)
    return (
      <StateMessage
        role="status"
        icon={<Cable />}
        title="Select a channel"
        description="Open a named channel from Channel analytics to inspect its traffic and performance."
      >
        <Button render={<Link href={uiHref("channels")} />}>Open channels</Button>
      </StateMessage>
    );
  const data = metrics.data;
  return (
    <div className="space-y-6 p-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <Link
            href={uiHref("channels")}
            className="mb-4 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-4" /> Back to channels
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Cable className="size-6" /> {data?.channel.name ?? "Channel details"}
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            {data && (
              <>
                <span>{data.channel.pool_name}</span>
                <StatusBadge
                  tone="neutral"
                  label={formatChannelConfigState(data.channel.status)}
                  tooltip="Saved channel configuration state. This does not measure upstream health."
                />
                <span>{data.channel.deployment_count} deployments</span>
              </>
            )}
            <code className="text-xs">{channelId}</code>
            <CopyButton value={channelId} label="Copy channel ID" />
          </div>
        </div>
        <MetricControls
          hours={hours}
          onHoursChange={(value) => void setRoute({ hours: value })}
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
        hasData={Boolean(data)}
      />
      {metrics.isPending && (
        <LoadingState
          title="Loading channel metrics"
          description="Reading recorded traffic, usage and measured timing data."
        />
      )}
      {data && (
        <>
          <p className="text-xs text-muted-foreground">
            Window: {formatChannelTimestamp(data.window.start)} to {formatChannelTimestamp(data.window.end)}
          </p>
          <RollingMetricStrip metrics={data.rolling} />
          <Tabs
            value={tab}
            onValueChange={(value) => {
              const next = channelDetailTabs.find((item) => item === value);
              if (next) void setRoute({ tab: next });
            }}
          >
            <TabsList variant="line" className="h-auto w-full justify-start border-b pb-1">
              {channelDetailTabs.map((value) => (
                <TabsTrigger key={value} value={value} className="flex-none px-4 py-2">
                  {tabLabels[value]}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value="overview" className="space-y-6 pt-4">
              <PeriodMetricGrid metrics={data.metrics} />
              <ChannelMetricCharts trend={data.trend} window={data.window} />
            </TabsContent>
            <TabsContent value="models" className="space-y-4 pt-4">
              <div>
                <h2 className="text-sm font-semibold">Model breakdown</h2>
                <p className="mt-1 text-xs leading-6 text-muted-foreground">
                  Rates use the last 60 seconds; request, spend and timing columns use the selected time window.
                  Additional measurements are available in View.
                </p>
              </div>
              <ChannelModelsTable models={data.models} />
            </TabsContent>
            <TabsContent value="errors" className="space-y-5 pt-4">
              <p className="text-sm leading-6 text-muted-foreground">
                {formatChannelCount(data.metrics.failed_requests)} failed requests ·{" "}
                {formatChannelCount(data.upstream_error_events)} upstream error events. Upstream events may include
                retries for a single request.
              </p>
              <div className="grid gap-5 lg:grid-cols-2">
                <ErrorGroups title="Request error categories" items={data.request_error_groups} />
                <ErrorGroups title="Upstream error events" items={data.upstream_error_groups} />
              </div>
              <RecentErrors items={data.recent_errors} />
            </TabsContent>
          </Tabs>
          <MetricCoverage coverage={data.coverage} notes={data.notes} />
        </>
      )}
    </div>
  );
}
