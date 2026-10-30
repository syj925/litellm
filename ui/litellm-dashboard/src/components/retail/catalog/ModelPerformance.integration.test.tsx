import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import ModelPerformance from "./ModelPerformance";
import type { ModelPerformanceMetrics, ModelPerformanceResponse } from "./types";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));

const emptyMetrics: ModelPerformanceMetrics = {
  requests: 0,
  successful_requests: 0,
  failed_requests: 0,
  unknown_status_requests: 0,
  success_rate_pct: null,
  input_tokens: null,
  output_tokens: null,
  total_tokens: null,
  latency_p50_ms: null,
  latency_p95_ms: null,
  ttft_p50_ms: null,
  ttft_p95_ms: null,
  output_tokens_per_second_p50: null,
  ttft_samples: 0,
  throughput_samples: 0,
};

function result(summary = emptyMetrics): ModelPerformanceResponse {
  return {
    model: "example-model",
    scope: "gateway",
    window: { start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z", hours: 24, bucket_minutes: 15 },
    summary,
    trend: [{ ...summary, time: "2026-01-01T00:00:00Z" }],
    notes: ["Only recorded requests are measured."],
  };
}

describe("model performance", () => {
  beforeEach(() => testQueryClient.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("uses the public observation endpoint without account credentials and explains absent data", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json(result()));
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(<ModelPerformance model="example-model" publicView accessToken="must-not-be-sent" />);
    expect(await screen.findByText("No completed request logs in this time window.")).toBeInTheDocument();
    expect(screen.getByText(/Non-streaming requests do not establish TTFT/)).toBeInTheDocument();
    expect(screen.getAllByText("Not available")).toHaveLength(3);
    expect(fetcher).toHaveBeenCalledWith(
      "/retail/public/model-metrics?model=example-model&hours=24",
      expect.anything(),
    );
    expect(new Headers((fetcher.mock.calls[0][1] as RequestInit).headers).has("Authorization")).toBe(false);
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
  });

  it("fetches a selected time range and scopes private observations to the authenticated account", async () => {
    const user = userEvent.setup();
    const fetcher = vi.fn().mockResolvedValue(Response.json({ ...result(), scope: "account" }));
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(<ModelPerformance model="example/model" publicView={false} accessToken="fixture-token" />);
    await screen.findByText("No completed request logs in this time window.");
    expect(screen.getByText(/your account's requests only/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "7 days" }));
    expect(fetcher).toHaveBeenCalledWith("/retail/model-metrics?model=example%2Fmodel&hours=168", expect.anything());
    const options = fetcher.mock.calls.find(([url]) => url.endsWith("hours=168"))?.[1] as RequestInit;
    expect(new Headers(options.headers).get("Authorization")).toBe("Bearer fixture-token");
    expect(screen.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows actual summary sample counts and does not substitute latency for missing streaming data", async () => {
    const observed = {
      ...emptyMetrics,
      requests: 3,
      successful_requests: 2,
      failed_requests: 1,
      success_rate_pct: 66.7,
      latency_p50_ms: 2500,
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(result(observed))));
    renderWithProviders(<ModelPerformance model="example-model" publicView />);
    const section = await screen.findByRole("region", { name: "Model performance" });
    expect(await within(section).findByText("66.7%")).toBeInTheDocument();
    expect(within(section).getAllByText("0 measured streaming samples")).toHaveLength(2);
    expect(within(section).getByText(/No valid output-speed samples/)).toBeInTheDocument();
    expect(within(section).queryByText("2.5 s")).not.toBeInTheDocument();
    expect(within(section).getByRole("heading", { name: "Request traffic" })).toBeInTheDocument();
  });

  it("shows measured streaming summaries when samples are present", async () => {
    const observed = {
      ...emptyMetrics,
      requests: 1,
      successful_requests: 1,
      success_rate_pct: 100,
      ttft_p50_ms: 1500,
      ttft_p95_ms: 1700,
      ttft_samples: 1,
      output_tokens_per_second_p50: 42,
      throughput_samples: 1,
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(result(observed))));
    renderWithProviders(<ModelPerformance model="example-model" publicView />);
    expect(await screen.findByText("42 tok/s")).toBeInTheDocument();
    expect(screen.getByText("1.5 s")).toBeInTheDocument();
    expect(screen.getAllByText("1 measured streaming samples")).toHaveLength(2);
    expect(screen.queryByText(/No valid streaming first-token samples/)).not.toBeInTheDocument();
  });

  it("does not display private backend diagnostics on error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ detail: "secret diagnostic" }, { status: 503 })));
    renderWithProviders(<ModelPerformance model="example-model" publicView />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Performance data is unavailable");
    expect(screen.queryByText("secret diagnostic")).not.toBeInTheDocument();
  });
});
