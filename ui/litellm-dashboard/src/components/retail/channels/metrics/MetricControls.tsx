"use client";

import { Pause, Play, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { channelHourLabels, channelHours, type ChannelHours } from "./route";
import { formatChannelTimestamp } from "./format";
import type { ChannelMetricCoverage } from "./types";

interface MetricControlsProps {
  hours: ChannelHours;
  onHoursChange: (hours: ChannelHours) => void;
  paused: boolean;
  onPausedChange: (paused: boolean) => void;
  isFetching: boolean;
  onRefresh: () => void;
}

export function MetricControls({
  hours,
  onHoursChange,
  paused,
  onPausedChange,
  isFetching,
  onRefresh,
}: MetricControlsProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        value={hours.toString()}
        onValueChange={(value) => {
          const next = channelHours.find((item) => item.toString() === value);
          if (next) onHoursChange(next);
        }}
      >
        <SelectTrigger aria-label="Channel metrics time range">
          <SelectValue>{channelHourLabels[hours]}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          {channelHours.map((value) => (
            <SelectItem key={value} value={value.toString()}>
              {channelHourLabels[value]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button variant="outline" onClick={() => onPausedChange(!paused)} aria-pressed={paused}>
        {paused ? <Play /> : <Pause />}
        {paused ? "Resume refresh" : "Pause refresh"}
      </Button>
      <Button
        variant="outline"
        size="icon"
        aria-label="Refresh channel metrics"
        disabled={isFetching}
        onClick={onRefresh}
      >
        <RefreshCw className={isFetching ? "animate-spin" : undefined} />
      </Button>
    </div>
  );
}

export function MetricFreshness({
  paused,
  updatedAt,
  error,
  hasData,
}: {
  paused: boolean;
  updatedAt: number;
  error: Error | null;
  hasData: boolean;
}) {
  return (
    <div className="space-y-3">
      <p className="text-xs leading-6 text-muted-foreground">
        {paused ? "Automatic refresh paused." : "Refreshes every 10 seconds while this page is visible."}
        {updatedAt > 0 && (
          <span> Last successful fetch: {formatChannelTimestamp(new Date(updatedAt).toISOString())}.</span>
        )}{" "}
        Rolling rates cover requests started in the last 60 seconds that have completed and reached the log database.
        Collection may lag live traffic.
      </p>
      {error && (
        <Alert variant="error">
          <AlertTitle>
            {hasData ? "Refresh failed; showing the last successful snapshot" : "Channel metrics could not be loaded"}
          </AlertTitle>
          <AlertDescription>{error.message}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}

export function MetricCoverage({ coverage, notes }: { coverage: ChannelMetricCoverage; notes: string[] }) {
  const proportion =
    coverage.model_id_coverage == null
      ? "—"
      : new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 }).format(
          coverage.model_id_coverage,
        );
  return (
    <section
      aria-label="Gateway-wide attribution coverage"
      className="rounded-lg border bg-muted/20 px-4 py-3 text-xs leading-6 text-muted-foreground"
    >
      <p className="font-medium text-foreground">Gateway-wide log attribution</p>
      <p>
        {coverage.attributed_requests.toLocaleString()} attributed · {coverage.unattributed_requests.toLocaleString()}{" "}
        unattributed · {coverage.window_log_rows.toLocaleString()} log rows · model ID coverage {proportion}
      </p>
      <p>
        Unattributed logs are excluded from channel statistics. Native gateway spend is a separate accounting basis from
        wallet revenue and supplier invoices.
      </p>
      {notes.length > 0 && (
        <ul className="mt-2 list-disc pl-4">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
