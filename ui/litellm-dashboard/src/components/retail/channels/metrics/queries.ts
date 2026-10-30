"use client";

import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { retailRequest } from "../../api";
import type { ChannelHours } from "./route";
import type { ChannelDetailMetricsResponse, ChannelMetricsResponse } from "./types";

interface MetricQueryOptions {
  accessToken: string | null;
  hours: ChannelHours;
  enabled: boolean;
  paused: boolean;
}

export function useChannelMetrics({ accessToken, hours, enabled, paused }: MetricQueryOptions) {
  const options: UseQueryOptions<ChannelMetricsResponse> = {
    queryKey: ["retail", "channel-metrics", hours],
    queryFn: ({ signal }) =>
      retailRequest<ChannelMetricsResponse>("/admin/channels/metrics?hours=" + hours, accessToken, { signal }),
    enabled: enabled && Boolean(accessToken),
    refetchInterval: paused ? false : 10_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: !paused,
    refetchOnReconnect: !paused,
    staleTime: 5_000,
    retry: false,
  };
  return useQuery(options);
}

export function useChannelDetailMetrics(
  channelId: string | null,
  { accessToken, hours, enabled, paused }: MetricQueryOptions,
) {
  const options: UseQueryOptions<ChannelDetailMetricsResponse> = {
    queryKey: ["retail", "channel-metrics", "detail", channelId, hours],
    queryFn: ({ signal }) =>
      retailRequest<ChannelDetailMetricsResponse>(
        "/admin/channels/" + encodeURIComponent(channelId ?? "") + "/metrics?hours=" + hours,
        accessToken,
        { signal },
      ),
    enabled: enabled && Boolean(accessToken) && Boolean(channelId),
    refetchInterval: paused ? false : 10_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: !paused,
    refetchOnReconnect: !paused,
    staleTime: 5_000,
    retry: false,
  };
  return useQuery(options);
}
