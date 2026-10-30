"use client";

import Link from "next/link";
import { type ColumnDef } from "@tanstack/react-table";
import { DataTable, DataTableSortHeader, DataTableToolbar } from "@/components/shared/DataTable";
import CopyButton from "@/components/shared/CopyButton";
import { StatusBadge } from "@/components/shared/table_cells";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { uiHref } from "@/utils/uiHref";
import { channelDetailHref, type ChannelHours } from "./route";
import { cacheDurationLabel, cacheTierEntries } from "../../cachePricing";
import {
  formatChannelCount,
  formatChannelConfigState,
  formatChannelDuration,
  formatChannelPercent,
  formatChannelTimestamp,
  formatChannelUsd,
} from "./format";
import type {
  ChannelErrorGroup,
  ChannelMetricItem,
  ChannelModelMetric,
  ChannelPeriodMetrics,
  ChannelRecentError,
} from "./types";

const numeric = { numeric: true } as const;

function periodColumn<T extends { metrics: ChannelPeriodMetrics }>(
  id: Exclude<keyof ChannelPeriodMetrics, "native_spend_usd" | "cache_write_ttl_tokens">,
  label: string,
  format: (value: number | null | undefined) => string,
): ColumnDef<T> {
  return {
    id,
    accessorFn: (row) => row.metrics[id],
    header: ({ column }) => <DataTableSortHeader column={column} title={label} />,
    meta: { ...numeric, title: label },
    cell: ({ row }) => format(row.original.metrics[id]),
  };
}

function cacheDurationsColumn<T extends { metrics: ChannelPeriodMetrics }>(): ColumnDef<T> {
  return {
    id: "cache_write_ttl_tokens",
    header: "Cache write by duration",
    meta: { title: "Cache write by duration" },
    cell: ({ row }) => (
      <div className="space-y-1 text-xs tabular-nums">
        {cacheTierEntries(row.original.metrics.cache_write_ttl_tokens).map(([ttl, tokens]) => (
          <p key={ttl}>{cacheDurationLabel(ttl)}: {formatChannelCount(tokens)}</p>
        ))}
        <p className="text-muted-foreground">
          Unspecified: {formatChannelCount(row.original.metrics.cache_write_unclassified_tokens)}
        </p>
      </div>
    ),
  };
}

function spendColumn<T extends { metrics: ChannelPeriodMetrics }>(): ColumnDef<T> {
  return {
    id: "native_spend_usd",
    accessorFn: (row) => (row.metrics.native_spend_usd == null ? null : Number(row.metrics.native_spend_usd)),
    header: ({ column }) => <DataTableSortHeader column={column} title="Gateway USD" />,
    meta: { ...numeric, title: "Gateway USD" },
    cell: ({ row }) => formatChannelUsd(row.original.metrics.native_spend_usd),
  };
}

export function channelSummaryColumns(hours: ChannelHours): ColumnDef<ChannelMetricItem>[] {
  return [
    {
      id: "channel",
      accessorFn: (row) => row.name,
      header: ({ column }) => <DataTableSortHeader column={column} title="Channel" />,
      meta: { title: "Channel" },
      size: 240,
      cell: ({ row }) => (
        <div>
          <Link href={channelDetailHref(row.original.id, hours)} className="font-medium hover:underline">
            {row.original.name}
          </Link>
          <p className="mt-1 text-xs text-muted-foreground">{row.original.pool_name}</p>
        </div>
      ),
    },
    {
      id: "status",
      accessorKey: "status",
      header: "Config state",
      meta: { title: "Config state" },
      cell: ({ row }) => (
        <StatusBadge
          tone={row.original.status === "active" ? "success" : "warning"}
          label={formatChannelConfigState(row.original.status)}
          tooltip="Saved channel configuration state. This does not measure upstream health."
        />
      ),
    },
    {
      id: "rpm",
      accessorFn: (row) => row.rolling.rpm,
      header: "RPM · 60s",
      meta: { ...numeric, title: "RPM · 60s" },
      cell: ({ row }) => formatChannelCount(row.original.rolling.rpm),
    },
    {
      id: "tpm",
      accessorFn: (row) => row.rolling.tpm,
      header: "TPM · 60s",
      meta: { ...numeric, title: "TPM · 60s" },
      cell: ({ row }) => formatChannelCount(row.original.rolling.tpm),
    },
    {
      id: "usd_min",
      accessorFn: (row) => row.rolling.native_usd_per_minute,
      header: "USD/min · 60s",
      meta: { ...numeric, title: "USD/min · 60s" },
      cell: ({ row }) => formatChannelUsd(row.original.rolling.native_usd_per_minute),
    },
    periodColumn("requests", "Requests", formatChannelCount),
    periodColumn("error_rate_pct", "Failure rate", formatChannelPercent),
    spendColumn(),
    periodColumn("total_tokens", "Tokens", formatChannelCount),
    periodColumn("latency_p50_ms", "Latency p50", formatChannelDuration),
    periodColumn("latency_p95_ms", "Latency p95", formatChannelDuration),
    periodColumn("ttft_p50_ms", "TTFT p50", formatChannelDuration),
    periodColumn("ttft_p95_ms", "TTFT p95", formatChannelDuration),
    periodColumn("cache_read_tokens", "Cache read", formatChannelCount),
    periodColumn("cache_write_tokens", "Cache write", formatChannelCount),
    cacheDurationsColumn(),
    {
      id: "upstream_error_events",
      accessorKey: "upstream_error_events",
      header: "Upstream events",
      meta: { ...numeric, title: "Upstream error events" },
      cell: ({ row }) => formatChannelCount(row.original.upstream_error_events),
    },
  ];
}

const modelColumns: ColumnDef<ChannelModelMetric>[] = [
  {
    id: "model",
    accessorKey: "model",
    header: ({ column }) => <DataTableSortHeader column={column} title="Model" />,
    meta: { title: "Model" },
    size: 260,
    cell: ({ row }) => (
      <span className="flex items-center gap-2 font-mono text-xs">
        {row.original.model}
        <CopyButton value={row.original.model} label="Copy model ID" />
      </span>
    ),
  },
  {
    id: "rpm",
    accessorFn: (row) => row.rolling.rpm,
    header: "RPM · 60s",
    meta: { ...numeric, title: "RPM · 60s" },
    cell: ({ row }) => formatChannelCount(row.original.rolling.rpm),
  },
  {
    id: "tpm",
    accessorFn: (row) => row.rolling.tpm,
    header: "TPM · 60s",
    meta: { ...numeric, title: "TPM · 60s" },
    cell: ({ row }) => formatChannelCount(row.original.rolling.tpm),
  },
  {
    id: "usd_min",
    accessorFn: (row) => row.rolling.native_usd_per_minute,
    header: "USD/min · 60s",
    meta: { ...numeric, title: "USD/min · 60s" },
    cell: ({ row }) => formatChannelUsd(row.original.rolling.native_usd_per_minute),
  },
  periodColumn("requests", "Requests", formatChannelCount),
  periodColumn("failed_requests", "Failed", formatChannelCount),
  periodColumn("error_rate_pct", "Failure rate", formatChannelPercent),
  spendColumn(),
  periodColumn("input_tokens", "Input", formatChannelCount),
  periodColumn("output_tokens", "Output", formatChannelCount),
  periodColumn("reasoning_tokens", "Reasoning", formatChannelCount),
  periodColumn("cache_read_tokens", "Cache read", formatChannelCount),
  periodColumn("cache_write_tokens", "Cache write", formatChannelCount),
  cacheDurationsColumn(),
  periodColumn("latency_p50_ms", "Latency p50", formatChannelDuration),
  periodColumn("latency_avg_ms", "Latency avg", formatChannelDuration),
  periodColumn("latency_p95_ms", "Latency p95", formatChannelDuration),
  periodColumn("ttft_p50_ms", "TTFT p50", formatChannelDuration),
  periodColumn("ttft_avg_ms", "TTFT avg", formatChannelDuration),
  periodColumn("ttft_p95_ms", "TTFT p95", formatChannelDuration),
  periodColumn("usage_samples", "Usage samples", formatChannelCount),
  periodColumn("ttft_samples", "TTFT samples", formatChannelCount),
];

export function ChannelModelsTable({ models }: { models: ChannelModelMetric[] }) {
  return (
    <DataTable
      data={models}
      columns={modelColumns}
      getRowId={(row) => row.model}
      sortingMode="client"
      defaultSorting={[{ id: "requests", desc: true }]}
      paginationMode="client"
      size="compact"
      noDataMessage="No attributed model activity in this time window."
      defaultColumnVisibility={{
        latency_avg_ms: false,
        ttft_avg_ms: false,
        usd_min: false,
        reasoning_tokens: false,
        cache_write_tokens: false,
        latency_p95_ms: false,
        ttft_p95_ms: false,
        usage_samples: false,
        ttft_samples: false,
      }}
      toolbar={(table) => <DataTableToolbar table={table} />}
    />
  );
}

export function ErrorGroups({ title, items }: { title: string; items: ChannelErrorGroup[] }) {
  return (
    <section className="min-w-0 rounded-xl border" aria-label={title}>
      <h3 className="border-b p-4 text-sm font-semibold">{title}</h3>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Category</TableHead>
            <TableHead>HTTP status</TableHead>
            <TableHead className="text-right">Count</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item) => (
            <TableRow key={item.error_type + ":" + item.status_code}>
              <TableCell className="max-w-64 truncate" title={item.error_type}>
                {item.error_type}
              </TableCell>
              <TableCell>{item.status_code ?? "—"}</TableCell>
              <TableCell className="text-right tabular-nums">{formatChannelCount(item.count)}</TableCell>
            </TableRow>
          ))}
          {!items.length && (
            <TableRow>
              <TableCell colSpan={3} className="py-8 text-center text-muted-foreground">
                No recorded errors in this category.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </section>
  );
}

export function RecentErrors({ items }: { items: ChannelRecentError[] }) {
  return (
    <section className="overflow-hidden rounded-xl border" aria-label="Recent channel errors">
      <h3 className="border-b p-4 text-sm font-semibold">
        Recent errors{" "}
        <span className="ml-2 font-normal text-muted-foreground">Latest {items.length} recorded summaries</span>
      </h3>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Source</TableHead>
            <TableHead>Request ID</TableHead>
            <TableHead>Model</TableHead>
            <TableHead>Category</TableHead>
            <TableHead>HTTP</TableHead>
            <TableHead>Summary</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((item, index) => (
            <TableRow key={item.source + item.request_id + item.time + index}>
              <TableCell className="text-xs">{formatChannelTimestamp(item.time)}</TableCell>
              <TableCell className="text-xs">{item.source === "request" ? "Request" : "Upstream event"}</TableCell>
              <TableCell>
                {item.request_id ? (
                  <div className="flex items-center gap-1">
                    {item.source === "request" ? (
                      <Link
                        href={uiHref("logs") + "?log_id=" + encodeURIComponent(item.request_id)}
                        className="max-w-36 truncate font-mono text-xs text-primary hover:underline"
                      >
                        {item.request_id}
                      </Link>
                    ) : (
                      <code className="max-w-36 truncate text-xs">{item.request_id}</code>
                    )}
                    <CopyButton
                      value={item.request_id}
                      label={item.source === "request" ? "Copy request ID" : "Copy upstream event ID"}
                    />
                  </div>
                ) : (
                  "—"
                )}
              </TableCell>
              <TableCell className="max-w-56 truncate font-mono text-xs" title={item.model}>
                {item.model || "—"}
              </TableCell>
              <TableCell className="text-xs">{item.error_type}</TableCell>
              <TableCell>{item.status_code ?? "—"}</TableCell>
              <TableCell className="max-w-md whitespace-normal text-xs leading-5">
                {item.message || "No summary recorded"}
              </TableCell>
            </TableRow>
          ))}
          {!items.length && (
            <TableRow>
              <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                No errors recorded in this time window.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </section>
  );
}
