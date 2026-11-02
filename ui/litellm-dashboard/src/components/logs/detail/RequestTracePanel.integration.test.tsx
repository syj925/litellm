import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequestTracePanel } from "./RequestTracePanel";
import type { RequestTraceDetail } from "./requestTrace";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function traceDetail(): RequestTraceDetail {
  return {
    call_id: "gateway-call",
    recorded: true,
    trace: {
      schema_version: 1,
      trace_id: "trace-1",
      call_id: "gateway-call",
      retail_request_id: "retail-1",
      started_at: "2026-11-02T00:00:00Z",
      finished_at: "2026-11-02T00:00:00.100Z",
      duration_ms: 100,
      method: "POST",
      path: "/v1/messages",
      request_body_bytes: 120,
      response_status: 200,
      attributes: {
        inbound_protocol: "anthropic",
        upstream_protocol: "openai",
        pool_id: "pool-1",
        pool_name: "Fixture pool",
        source_kind: "openai",
      },
      milestones: {
        request_received: 0,
        upstream_started: 5,
        upstream_first_event: 25,
        first_reasoning_output: 30,
        first_text_output: 75,
        first_text_write: 80,
      },
      phases: [
        { name: "authentication", start_ms: 0, end_ms: 2, duration_ms: 2, status: "ok", samples: 1000 },
        { name: "routing", start_ms: 2, end_ms: 5, duration_ms: null, status: "ok" },
      ],
      events: [{ name: "route_selected", at_ms: 5, attributes: { deployment_id: "deployment-2" } }],
      attempts: [
        {
          attempt: 1,
          started_ms: 5,
          ended_ms: 20,
          attributes: {
            provider: "openai",
            model: "fixture",
            deployment_id: "deployment-1",
            api_base_origin: "https://first.example",
          },
          status_code: 429,
          error_type: "RateLimitError",
          upstream_request_id: "upstream-first",
          events: [],
        },
        {
          attempt: 2,
          started_ms: 21,
          ended_ms: 95,
          attributes: {
            provider: "openai",
            model: "fixture",
            deployment_id: "deployment-2",
            api_base_origin: "https://second.example",
          },
          status_code: 200,
          error_type: null,
          upstream_request_id: "upstream-second",
          events: [],
        },
      ],
      truncated: false,
    },
    request_body: {
      available: true,
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      value: { messages: [{ role: "user", content: "[redacted]" }] },
    },
    billing: {
      retail_request_id: "retail-1",
      status: "settled",
      reserved_amount: "0.1",
      settled_amount: "0.012",
      currency: "USD",
      price_snapshot: { version: "expression-v1", price_version: 7 },
      usage: { input_tokens: 10, output_tokens: 2 },
      cost_breakdown: { expression: "0.012" },
      provider_cost: null,
      provider_cost_source: null,
      expression_pricing: {
        tiers: ["Standard short context"],
        discount: "0.5",
        undiscounted_amount: "0.024",
        amount: "0.012",
        rounding_adjustment: "0.000000001",
        started_at: "2026-11-02T00:00:00Z",
        timezone: "UTC",
      },
      ledger: [{ id: "ledger-1", kind: "charge", amount: "0.012", created_at: "2026-11-02T00:00:00.100Z" }],
      resource: { pool_id: "pool-1", pool_name: "Fixture pool", source_kind: "openai", routing_settings_version: 3 },
    },
    missing: ["provider_cost"],
  };
}

function serve(detail: RequestTraceDetail) {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(detail), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

describe("request trace panel", () => {
  it("shows recorded stages, attempts, protocol conversion and linked billing evidence without estimating missing timings", async () => {
    serve(traceDetail());
    render(<RequestTracePanel accessToken="admin-token" callId="gateway-call" />);
    const stageSummary = await screen.findByText("Recorded stages · 2");
    const attemptSummary = screen.getByText("Upstream attempts · 2 · HTTP 429 / HTTP 200");
    expect(stageSummary).toBeVisible();
    expect(attemptSummary).toBeVisible();
    expect(screen.getByRole("table", { name: "Recorded request stages" })).not.toBeVisible();
    expect(screen.getByText("upstream-first")).not.toBeVisible();
    expect(screen.getByText("POST /v1/messages")).toBeVisible();
    expect(screen.getByText("anthropic")).toBeVisible();
    const timeline = screen.getByRole("region", { name: "Recorded response timeline" });
    expect(within(timeline).getByText("First upstream text")).toBeVisible();
    expect(within(timeline).getByText("75 ms")).toBeVisible();
    expect(screen.getByText("retail-1")).toBeVisible();
    expect(screen.getByText("ledger-1")).toBeVisible();
    expect(screen.getByRole("table", { name: "Recorded billable usage" })).toHaveTextContent("Uncached input tokens10");
    expect(screen.getByRole("region", { name: "Frozen pricing and settlement" })).toHaveTextContent("0.5 × base price");
    expect(screen.getByText("Standard short context")).toBeVisible();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
    fireEvent.click(stageSummary);
    const stages = screen.getByRole("table", { name: "Recorded request stages" });
    expect(within(stages).getByRole("row", { name: /Authentication/ })).toHaveTextContent("2 ms");
    expect(within(stages).getByRole("row", { name: /Routing/ })).toHaveTextContent("Not recorded");
    expect(screen.getByText("Cumulative · 1000 measurements")).toBeVisible();
    fireEvent.click(attemptSummary);
    expect(screen.getByText("HTTP 429")).toBeVisible();
    expect(screen.getByText("HTTP 200")).toBeVisible();
    expect(screen.getByText("upstream-first")).toBeVisible();
    expect(screen.getByText("upstream-second")).toBeVisible();
    fireEvent.click(screen.getByText(/Redacted request body · Available/));
    expect(screen.getByText(/Request content is redacted and retained for 30 minutes/)).toBeVisible();
  });

  it("labels gateway cost calculations as estimates instead of supplier invoice prices", async () => {
    const detail = traceDetail();
    serve({
      ...detail,
      billing: { ...detail.billing!, provider_cost: "0.004", provider_cost_source: "gateway_cost_calculation" },
    });
    render(<RequestTracePanel accessToken="admin-token" callId="gateway-call" />);
    expect(await screen.findByText("Gateway cost estimate (USD)")).toBeVisible();
    expect(screen.getByText("Calculated by the gateway; supplier invoice cost is not recorded.")).toBeVisible();
    expect(screen.queryByText("Recorded provider cost (USD)")).not.toBeInTheDocument();
  });

  it("identifies historical uncollected stages and keeps expired content out of the page", async () => {
    const detail = traceDetail();
    serve({
      ...detail,
      recorded: false,
      trace: null,
      request_body: {
        ...detail.request_body,
        expires_at: "2020-01-01T00:00:00Z",
        value: { secret: "stale-body-content" },
      },
      billing: null,
    });
    render(<RequestTracePanel accessToken="admin-token" callId="legacy-call" />);
    expect(await screen.findByText(/Detailed tracing was not captured/)).toBeVisible();
    expect(screen.queryByRole("table", { name: "Recorded request stages" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/Redacted request body · Unavailable/));
    expect(screen.getByText("Request body expired or was not captured.")).toBeVisible();
    expect(screen.queryByText("stale-body-content")).not.toBeInTheDocument();
  });

  it("removes redacted request content from the rendered page at its expiry", async () => {
    vi.useFakeTimers();
    const detail = traceDetail();
    serve({
      ...detail,
      request_body: { ...detail.request_body, expires_at: new Date(Date.now() + 1000).toISOString() },
    });
    await act(async () => {
      render(<RequestTracePanel accessToken="admin-token" callId="gateway-call" />);
    });
    expect(screen.getByText(/Redacted request body · Available/)).toBeVisible();
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByText(/Redacted request body · Unavailable/)).toBeVisible();
  });
});
