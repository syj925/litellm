import { retailRequest } from "@/components/retail/api";

export type TraceValue = string | number | boolean | null | TraceValue[] | { [key: string]: TraceValue };
export type TraceAttributes = Record<string, string | number | boolean | null>;

export interface RequestTraceEvent {
  name: string;
  at_ms: number;
  attributes: TraceAttributes;
}

export interface RequestTraceSnapshot {
  schema_version: number;
  trace_id: string;
  call_id: string | null;
  retail_request_id: string | null;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  method: string;
  path: string;
  request_body_bytes: number | null;
  response_status: number | null;
  attributes: TraceAttributes;
  milestones: Record<string, number>;
  phases: {
    name: string;
    start_ms: number;
    end_ms: number | null;
    duration_ms: number | null;
    status: "ok" | "error";
    samples?: number;
  }[];
  events: RequestTraceEvent[];
  attempts: {
    attempt: number;
    started_ms: number;
    ended_ms: number | null;
    attributes: TraceAttributes;
    status_code: number | null;
    error_type: string | null;
    upstream_request_id: string | null;
    events: RequestTraceEvent[];
  }[];
  truncated: boolean;
}

export interface RequestTraceBilling {
  retail_request_id: string;
  status: string;
  reserved_amount: string | null;
  settled_amount: string | null;
  currency: "USD";
  price_snapshot: Record<string, TraceValue> | null;
  usage: Record<string, TraceValue> | null;
  cost_breakdown: Record<string, TraceValue> | null;
  provider_cost: string | null;
  provider_cost_source: string | null;
  expression_pricing?: {
    tiers?: string[];
    variables?: Record<string, TraceValue>;
    usage?: Record<string, TraceValue>;
    undiscounted_amount?: string;
    discount?: string;
    amount?: string;
    rounding_adjustment?: string;
    timezone?: string;
    started_at?: string;
    expression_hash?: string;
    expression?: string;
  } | null;
  ledger: { id: string; kind: string; amount: string; created_at: string }[];
  resource: {
    pool_id: string | null;
    pool_name: string | null;
    source_kind: string | null;
    routing_settings_version: number | null;
  } | null;
}

export interface RequestTraceDetail {
  call_id: string;
  recorded: boolean;
  trace: RequestTraceSnapshot | null;
  request_body: { available: boolean; expires_at: string | null; value: TraceValue };
  billing: RequestTraceBilling | null;
  missing: string[];
}

export function requestTraceDetailsCall(
  accessToken: string,
  callId: string,
  signal?: AbortSignal,
): Promise<RequestTraceDetail> {
  return retailRequest<RequestTraceDetail>(`/admin/request-traces/${encodeURIComponent(callId)}`, accessToken, {
    signal,
  });
}

const eventLabels: Readonly<Record<string, string>> = {
  request_received: "Request received",
  upstream_started: "First upstream request started",
  request_body_complete: "Request body received",
  upstream_first_event: "First upstream event",
  first_model_output: "First model output",
  first_text_output: "First upstream text",
  first_reasoning_output: "First reasoning output",
  first_tool_output: "First tool output",
  first_model_write: "First model output accepted by ASGI",
  first_text_write: "First text accepted by ASGI",
  first_client_write: "First response bytes accepted by ASGI",
  response_headers: "Response headers sent",
  response_complete: "Response complete",
  authentication: "Authentication",
  authorization: "Authorization",
  routing: "Routing",
  reservation: "Wallet reservation",
  settlement: "Wallet settlement",
  "request.body_receive": "Read request body",
  "request.json_parse": "Parse request JSON",
  "request.pre_call_hooks": "Prepare request and run hooks",
  "resource_pool.resolve": "Select resource pool",
  "billing.prepare": "Prepare billing and freeze prices",
  "billing.reserve": "Reserve wallet balance",
  "billing.settle": "Settle final charge",
  "billing.failure": "Handle billing after failure",
  protocol_route: "Upstream protocol and path",
  route_selected: "Route selected",
  "client.write": "ASGI response writes",
};

export function traceEventLabel(name: string): string {
  if (name.startsWith("auth ")) return `Authentication · ${name.slice(5)}`;
  return eventLabels[name] ?? name.replaceAll("_", " ");
}

export function traceMilliseconds(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "Not recorded";
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value)} ms`;
}

export function recordedIntervalMs(start: number | null | undefined, end: number | null | undefined): number | null {
  if (start == null || end == null || !Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return end - start;
}

export function attemptTransportIntervals(events: RequestTraceEvent[]): { label: string; duration: number | null }[] {
  const groups = [
    ["TCP connection", ["connection.connect_tcp"]],
    ["TLS handshake", ["connection.start_tls"]],
    ["Send request headers", ["http11.send_request_headers", "http2.send_request_headers"]],
    ["Send request body", ["http11.send_request_body", "http2.send_request_body"]],
    ["Receive response headers", ["http11.receive_response_headers", "http2.receive_response_headers"]],
    ["Read response body", ["response_body"]],
  ] as const;
  return groups.map(([label, prefixes]) => {
    const prefix = prefixes.find((candidate) => events.some((event) => event.name === `${candidate}.started`));
    const start = events.find((event) => event.name === `${prefix}.started`);
    const end = events.find(
      (event) =>
        event.at_ms >= (start?.at_ms ?? Infinity) &&
        (event.name === `${prefix}.complete` || event.name === `${prefix}.failed`),
    );
    return { label, duration: recordedIntervalMs(start?.at_ms, end?.at_ms) };
  });
}

export function traceTimestamp(value: string | null | undefined): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not recorded" : date.toISOString();
}

export function traceScalar(value: string | number | boolean | null | undefined): string {
  if (value == null || value === "") return "Not recorded";
  return String(value);
}

export function requestBodyIsAvailable(body: RequestTraceDetail["request_body"], now = Date.now()): boolean {
  if (!body.available || body.value == null) return false;
  if (!body.expires_at) return true;
  const expires = Date.parse(body.expires_at);
  return Number.isFinite(expires) && expires > now;
}
