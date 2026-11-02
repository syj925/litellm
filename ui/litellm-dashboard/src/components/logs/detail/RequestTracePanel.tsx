import { useEffect, useState } from "react";
import CopyButton from "@/components/shared/CopyButton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { JsonViewer } from "./JsonViewer";
import { TraceBillingDetails } from "./TraceBillingDetails";
import { TraceMilestoneTimeline } from "./TraceMilestoneTimeline";
import {
  attemptTransportIntervals,
  recordedIntervalMs,
  requestBodyIsAvailable,
  requestTraceDetailsCall,
  traceEventLabel,
  traceMilliseconds,
  traceScalar,
  traceTimestamp,
  type RequestTraceDetail,
  type RequestTraceEvent,
  type RequestTraceSnapshot,
} from "./requestTrace";

interface TraceState {
  callId: string;
  refresh: number;
  detail: RequestTraceDetail | null;
  error: string | null;
}

function TraceEvents({ events }: { events: RequestTraceEvent[] }) {
  if (!events.length) return <p className="text-xs text-muted-foreground">No events were recorded.</p>;
  return (
    <Table aria-label="Recorded trace events">
      <TableHeader>
        <TableRow>
          <TableHead>Event</TableHead>
          <TableHead>Since request received</TableHead>
          <TableHead>Recorded facts</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {events.map((event, index) => (
          <TableRow key={index}>
            <TableCell>{traceEventLabel(event.name)}</TableCell>
            <TableCell className="whitespace-nowrap tabular-nums">{traceMilliseconds(event.at_ms)}</TableCell>
            <TableCell className="max-w-80 whitespace-normal text-xs">
              {Object.entries(event.attributes).map(([key, value]) => (
                <p key={key} className="break-all">
                  {key.replaceAll("_", " ")}: {traceScalar(value)}
                </p>
              ))}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function TraceStages({ trace }: { trace: RequestTraceSnapshot }) {
  return (
    <div className="space-y-3">
      {trace.phases.length ? (
        <Table aria-label="Recorded request stages">
          <TableHeader>
            <TableRow>
              <TableHead>Stage</TableHead>
              <TableHead>Start offset</TableHead>
              <TableHead>End offset</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {trace.phases.map((phase, index) => (
              <TableRow key={index}>
                <TableCell>{traceEventLabel(phase.name)}</TableCell>
                <TableCell className="tabular-nums">{traceMilliseconds(phase.start_ms)}</TableCell>
                <TableCell className="tabular-nums">{traceMilliseconds(phase.end_ms)}</TableCell>
                <TableCell className="tabular-nums">
                  {traceMilliseconds(phase.duration_ms)}
                  {phase.samples != null && phase.samples > 1 && (
                    <p className="mt-1 text-xs text-muted-foreground">Cumulative · {phase.samples} measurements</p>
                  )}
                </TableCell>
                <TableCell>
                  <Badge variant={phase.status === "error" ? "destructive" : "secondary"}>
                    {phase.status === "ok" ? "Completed" : "Error"}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <p className="text-xs text-muted-foreground">Stage durations were not recorded for this request.</p>
      )}
      <p className="text-xs leading-5 text-muted-foreground">
        Offsets start at gateway request receipt. Stages may overlap and cumulative writes are not sequential; do not
        add their durations. Missing stages are not estimated from total duration.
      </p>
      <details className="rounded-lg border">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
          Response milestones · {Object.keys(trace.milestones).length}
        </summary>
        <dl className="grid gap-3 border-t p-3 text-xs sm:grid-cols-2">
          {Object.entries(trace.milestones)
            .sort((left, right) => left[1] - right[1])
            .map(([name, value]) => (
              <div key={name}>
                <dt className="text-muted-foreground">{traceEventLabel(name)}</dt>
                <dd className="mt-1 tabular-nums">{traceMilliseconds(value)}</dd>
              </div>
            ))}
        </dl>
      </details>
    </div>
  );
}

function TraceAttempts({ attempts }: { attempts: RequestTraceSnapshot["attempts"] }) {
  return (
    <div className="space-y-3">
      {!attempts.length && (
        <p className="text-xs text-muted-foreground">
          No upstream attempt was recorded. This does not establish that no upstream call occurred.
        </p>
      )}
      {attempts.map((attempt) => (
        <div key={attempt.attempt} className="space-y-3 rounded-lg border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <p className="font-medium">
              Attempt {attempt.attempt} · {traceScalar(attempt.attributes.provider)}
            </p>
            <Badge
              variant={
                attempt.error_type || (attempt.status_code != null && attempt.status_code >= 400)
                  ? "destructive"
                  : "secondary"
              }
            >
              {attempt.status_code == null ? "Status not recorded" : `HTTP ${attempt.status_code}`}
            </Badge>
          </div>
          <dl className="grid gap-3 text-xs sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">Start / end offset</dt>
              <dd className="mt-1 tabular-nums">
                {traceMilliseconds(attempt.started_ms)} / {traceMilliseconds(attempt.ended_ms)}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Elapsed between recorded start and end</dt>
              <dd className="mt-1 tabular-nums">
                {traceMilliseconds(recordedIntervalMs(attempt.started_ms, attempt.ended_ms))}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Model / deployment</dt>
              <dd className="mt-1 break-all">
                {traceScalar(attempt.attributes.model)} / {traceScalar(attempt.attributes.deployment_id)}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Upstream request ID</dt>
              <dd className="mt-1 flex items-center gap-1">
                <span className="break-all font-mono">{traceScalar(attempt.upstream_request_id)}</span>
                {attempt.upstream_request_id && (
                  <CopyButton value={attempt.upstream_request_id} label="Copy upstream request ID" />
                )}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Recorded error</dt>
              <dd className="mt-1 break-all">{traceScalar(attempt.error_type)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Upstream origin</dt>
              <dd className="mt-1 break-all">{traceScalar(attempt.attributes.api_base_origin)}</dd>
            </div>
          </dl>
          <div className="border-t pt-3">
            <p className="mb-2 text-xs font-medium">Connection and transfer timings</p>
            <dl className="grid gap-3 text-xs sm:grid-cols-2">
              {attemptTransportIntervals(attempt.events).map((interval) => (
                <div key={interval.label}>
                  <dt className="text-muted-foreground">{interval.label}</dt>
                  <dd className="mt-1 tabular-nums">{traceMilliseconds(interval.duration)}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">
              Only paired transport events are measured. Connection details may be unavailable for this HTTP client or a
              reused connection. Body reading includes upstream streaming time.
            </p>
          </div>
          {attempt.events.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs font-medium">Attempt events · {attempt.events.length}</summary>
              <TraceEvents events={attempt.events} />
            </details>
          )}
        </div>
      ))}
    </div>
  );
}

function RecordedTrace({ trace }: { trace: RequestTraceSnapshot }) {
  const attributes = trace.attributes;
  const routeFields = [
    ["Inbound protocol", attributes.inbound_protocol],
    ["Upstream protocol", attributes.upstream_protocol],
    [
      "Resource pool",
      attributes.resource_pool_name ?? attributes.pool_name ?? attributes.resource_pool_id ?? attributes.pool_id,
    ],
    ["Resource source", attributes.resource_source_kind ?? attributes.source_kind],
  ] as const;
  return (
    <div className="space-y-5">
      {trace.truncated && (
        <p role="status" className="rounded-md border p-3 text-xs text-muted-foreground">
          The capture limit was reached. Some events were not recorded.
        </p>
      )}
      <dl className="grid gap-3 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-muted-foreground">Original request</dt>
          <dd className="mt-1 break-all font-mono text-xs">
            {trace.method} {trace.path}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Total recorded duration</dt>
          <dd className="mt-1 tabular-nums">{traceMilliseconds(trace.duration_ms)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Received at (UTC)</dt>
          <dd className="mt-1 break-all text-xs">{traceTimestamp(trace.started_at)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Completed at (UTC)</dt>
          <dd className="mt-1 break-all text-xs">{traceTimestamp(trace.finished_at)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Request body size</dt>
          <dd className="mt-1">
            {trace.request_body_bytes == null ? "Not recorded" : `${trace.request_body_bytes.toLocaleString()} bytes`}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Gateway response status</dt>
          <dd className="mt-1">{traceScalar(trace.response_status)}</dd>
        </div>
        {routeFields.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="mt-1 break-all">{traceScalar(value)}</dd>
          </div>
        ))}
      </dl>
      <TraceMilestoneTimeline trace={trace} />
      <details className="rounded-lg border">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
          Recorded stages · {trace.phases.length}
        </summary>
        <div className="border-t p-3">
          <TraceStages trace={trace} />
        </div>
      </details>
      <details className="rounded-lg border">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
          Upstream attempts · {trace.attempts.length}
          {trace.attempts.length > 0 &&
            ` · ${trace.attempts
              .map((attempt) => (attempt.status_code == null ? "Status not recorded" : `HTTP ${attempt.status_code}`))
              .join(" / ")}`}
        </summary>
        <div className="border-t p-3">
          <TraceAttempts attempts={trace.attempts} />
        </div>
      </details>
      <details className="rounded-lg border">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
          Routing decisions and recorded events · {trace.events.length}
        </summary>
        <TraceEvents events={trace.events} />
      </details>
      <p className="break-all text-xs text-muted-foreground">Trace ID: {trace.trace_id}</p>
    </div>
  );
}

function RequestTraceDetails({ detail }: { detail: RequestTraceDetail }) {
  const [expiredBody, setExpiredBody] = useState<RequestTraceDetail["request_body"] | null>(null);
  const body = detail.request_body;
  useEffect(() => {
    if (!body.expires_at || !body.available) return;
    const remaining = Date.parse(body.expires_at) - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) return;
    const timer = setTimeout(() => setExpiredBody(body), remaining);
    return () => clearTimeout(timer);
  }, [body]);
  const available = expiredBody !== body && requestBodyIsAvailable(body);
  return (
    <div className="space-y-5">
      {detail.recorded && detail.trace ? (
        <RecordedTrace trace={detail.trace} />
      ) : (
        <p role="status" className="rounded-lg border bg-muted/20 p-3 text-sm text-muted-foreground">
          Detailed tracing was not captured for this call. Historical logs retain their existing metrics; missing stage
          times are not reconstructed.
        </p>
      )}
      <details className="rounded-lg border">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
          Redacted request body · {available ? "Available" : "Unavailable"}
        </summary>
        <div className="border-t px-3 py-3">
          <p className="mb-2 text-xs text-muted-foreground">
            Request content is redacted and retained for 30 minutes. Expires at (UTC): {traceTimestamp(body.expires_at)}
          </p>
          {available ? (
            <JsonViewer data={body.value} mode="formatted" />
          ) : (
            <p className="text-sm text-muted-foreground">Request body expired or was not captured.</p>
          )}
        </div>
      </details>
      <details className="rounded-lg border" open={Boolean(detail.billing)}>
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
          Retail billing association · {detail.billing?.status ?? "Not recorded"}
        </summary>
        <div className="border-t p-3">
          <TraceBillingDetails billing={detail.billing} />
        </div>
      </details>
      {detail.missing.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Not recorded: {detail.missing.map((value) => value.replaceAll("_", " ")).join(", ")}
        </p>
      )}
    </div>
  );
}

export function RequestTracePanel({ accessToken, callId }: { accessToken: string; callId: string }) {
  const [state, setState] = useState<TraceState>({ callId, refresh: 0, detail: null, error: null });
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    requestTraceDetailsCall(accessToken, callId, controller.signal)
      .then((detail) => {
        if (!controller.signal.aborted) setState({ callId, refresh, detail, error: null });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({
            callId,
            refresh,
            detail: null,
            error: error instanceof Error ? error.message : "Request trace is unavailable",
          });
      });
    return () => controller.abort();
  }, [accessToken, callId, refresh]);
  const current = state.callId === callId && state.refresh === refresh ? state : null;
  return (
    <Card size="sm" className="mb-6" role="region" aria-label="Administrator request trace">
      <CardHeader className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle>Request trace</CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">
            Administrator view · recorded request stages, upstream attempts and billing evidence
          </p>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={() => setRefresh((value) => value + 1)}>
          Refresh trace
        </Button>
      </CardHeader>
      <CardContent>
        {!current?.detail && !current?.error && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading recorded trace…
          </p>
        )}
        {current?.error && (
          <p role="alert" className="text-sm text-destructive">
            Unable to load request trace: {current.error}
          </p>
        )}
        {current?.detail && <RequestTraceDetails key={refresh} detail={current.detail} />}
      </CardContent>
    </Card>
  );
}
