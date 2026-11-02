import { recordedIntervalMs, traceEventLabel, traceMilliseconds, type RequestTraceSnapshot } from "./requestTrace";

const visibleMilestones = [
  "request_received",
  "upstream_started",
  "upstream_first_event",
  "first_model_output",
  "first_text_output",
  "first_model_write",
  "first_text_write",
  "response_complete",
] as const;

export function TraceMilestoneTimeline({ trace }: { trace: RequestTraceSnapshot }) {
  const points = trace.milestones;
  const maximum = Math.max(trace.duration_ms ?? 0, ...Object.values(points).filter(Number.isFinite), 1);
  const intervals = [
    ["Gateway before upstream", "request_received", "upstream_started"],
    ["Upstream start to first event", "upstream_started", "upstream_first_event"],
    ["Upstream start to first model output", "upstream_started", "first_model_output"],
    ["Upstream start to first text", "upstream_started", "first_text_output"],
    ["First model output to ASGI write", "first_model_output", "first_model_write"],
  ] as const;
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-label="Recorded response timeline">
      <div>
        <h4 className="font-medium">Response timeline</h4>
        <p className="mt-1 text-xs text-muted-foreground">Actual offsets since gateway request receipt</p>
      </div>
      <ol className="space-y-3">
        {visibleMilestones.map((name) => {
          const value = points[name];
          const known = value != null && Number.isFinite(value) && value >= 0;
          return (
            <li key={name} className="space-y-1">
              <div className="flex flex-wrap justify-between gap-2 text-xs">
                <span>{traceEventLabel(name)}</span>
                <span className="tabular-nums text-muted-foreground">{traceMilliseconds(value)}</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                {known && (
                  <div
                    className="h-full min-w-0.5 rounded-full bg-info"
                    style={{ width: `${Math.min(100, (value / maximum) * 100)}%` }}
                  />
                )}
              </div>
            </li>
          );
        })}
      </ol>
      <dl className="grid gap-3 border-t pt-3 text-xs sm:grid-cols-2">
        {intervals.map(([label, start, end]) => (
          <div key={label}>
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="mt-1 font-medium tabular-nums">
              {traceMilliseconds(recordedIntervalMs(points[start], points[end]))}
            </dd>
          </div>
        ))}
      </dl>
      <p className="text-xs leading-5 text-muted-foreground">
        Reasoning or tool output can precede text. Write timings end when ASGI accepts bytes; they do not measure when
        the client receives or renders them.
      </p>
    </section>
  );
}
