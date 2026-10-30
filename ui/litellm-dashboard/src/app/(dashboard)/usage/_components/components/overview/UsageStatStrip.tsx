"use client";

import React, { useMemo } from "react";
import type { DailyData } from "@/components/UsagePage/types";
import { cn } from "@/lib/cva.config";
import { dailyTotals, formatCompact, formatLatency, formatUsd, type OverviewTotals } from "./overviewData";
import { PANEL_INSET_X, Sparkline, Stat } from "./Primitives";
import Link from "next/link";
import { uiHref } from "@/utils/uiHref";

const BRAND = "#2b3fd6";

interface UsageStatStripProps {
  results: readonly DailyData[];
  totals: OverviewTotals;
  loading: boolean;
  requestCountsPending: boolean;
  budget: number | null;
  showCosts?: boolean;
}

/** Spend, requests, tokens and averages for a range, each with a daily sparkline. */
export default function UsageStatStrip({
  results,
  totals,
  loading,
  requestCountsPending,
  budget,
  showCosts = true,
}: UsageStatStripProps) {
  const daily = useMemo(() => dailyTotals(results), [results]);
  return (
    <div className="grid grid-cols-1 overflow-hidden rounded-xl border bg-card sm:grid-cols-2 lg:grid-cols-4 lg:divide-x">
      {showCosts ? (
        <StatCell trend={<Sparkline data={daily} dataKey="spend" color={BRAND} className="h-14" />}>
          <Stat
            label="Spend"
            value={formatUsd(totals.spend)}
            pending={loading}
            hint={budget ? `of ${formatUsd(budget)} budget` : "No budget set"}
          />
        </StatCell>
      ) : (
        <StatCell>
          <p className="text-sm font-medium">Your charges</p>
          <Link href={uiHref("billing")} className="mt-2 inline-block text-sm font-medium text-info underline">
            View Billing
          </Link>
          <p className="mt-1 text-xs text-muted-foreground">
            Actual charges after your discount are recorded in Billing.
          </p>
        </StatCell>
      )}
      <StatCell trend={<Sparkline data={daily} dataKey="requests" color={BRAND} className="h-14" />}>
        <Stat label="Total Requests" value={totals.requests.toLocaleString()} pending={requestCountsPending} />
        {!requestCountsPending && (
          <div className="mt-0.5 flex gap-3 text-xs leading-4 text-muted-foreground">
            <span>
              <span className="text-success tabular-nums">{totals.successful.toLocaleString()}</span> ok
            </span>
            <span>
              <span className={totals.failed > 0 ? "text-destructive tabular-nums" : "tabular-nums"}>
                {totals.failed.toLocaleString()}
              </span>{" "}
              failed
            </span>
          </div>
        )}
      </StatCell>
      <StatCell trend={<Sparkline data={daily} dataKey="tokens" color={BRAND} className="h-14" />}>
        <Stat
          label="Total Tokens"
          value={loading ? null : formatCompact(totals.tokens)}
          exact={`${totals.tokens.toLocaleString()} tokens`}
          pending={loading}
          hint={loading ? undefined : `${formatCompact(totals.cacheReadTokens)} cache read`}
        />
      </StatCell>
      <StatCell>
        <div className="grid gap-3">
          {showCosts && (
            <Stat label="Avg cost / request" value={formatUsd(totals.avgCostPerRequest, 4)} pending={loading} />
          )}
          <Stat
            label="Avg latency"
            value={totals.avgLatencyMs === null ? "—" : formatLatency(totals.avgLatencyMs)}
            pending={loading}
            hint={totals.successRate === null ? undefined : `${totals.successRate.toFixed(1)}% success rate`}
          />
        </div>
      </StatCell>
    </div>
  );
}

function StatCell({ children, trend }: { children: React.ReactNode; trend?: React.ReactNode }) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col justify-between gap-3 border-b py-4 last:border-b-0 lg:border-b-0",
        PANEL_INSET_X,
      )}
    >
      <div className="min-w-0">{children}</div>
      {trend}
    </div>
  );
}
