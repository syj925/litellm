import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CacheUsageMetricsPanel from "./CacheUsageMetricsPanel";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/components/networking", () => ({
  apiClient: { get },
  formatDate: (date: Date) =>
    `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`,
}));

const props = {
  accessToken: "test-token",
  startTime: new Date(2026, 0, 1, 12),
  endTime: new Date(2026, 0, 3, 12),
  scope: "global" as const,
  userId: "selected-user",
};
const metrics = {
  cache_read_tokens: 105,
  cache_write_tokens: 150,
  cache_write_ttl_tokens: { "300": 11, "750": 12, "1800": 13, "3600": 14, "43200": 15 },
  cache_write_unclassified_tokens: 85,
  request_count: 5,
  cache_usage_samples: 3,
  cache_usage_unknown_requests: 2,
};

function provider() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("usage cache duration summary", () => {
  beforeEach(() => {
    get.mockReset();
  });

  it("keeps native day buckets, selected account and exact model-group keys together", async () => {
    get.mockResolvedValue(metrics);
    render(<CacheUsageMetricsPanel {...props} modelKeys={["matching/model"]} />, { wrapper: provider() });
    expect(await screen.findByText("Cache write · 12h")).toBeInTheDocument();
    expect(screen.getByText("Cache write · 5m")).toBeInTheDocument();
    expect(screen.getByText("Cache write · 30m")).toBeInTheDocument();
    expect(screen.getByText("Cache write · 1h")).toBeInTheDocument();
    expect(screen.getByText("150")).toBeInTheDocument();
    expect(screen.getByText("85")).toBeInTheDocument();
    expect(screen.getByText(/3 of 5 final requests/)).toHaveTextContent("2 requests have incomplete cache counts");
    expect(get).toHaveBeenCalledWith(
      "/retail/cache-usage",
      expect.objectContaining({
        query: {
          start_date: "2026-01-01T00:00:00.000Z",
          end_date: "2026-01-04T00:00:00.000Z",
          scope: "global",
          user_id: "selected-user",
          model_groups: ["matching/model"],
        },
      }),
    );
  });

  it("removes the previous account totals while the new filter is pending", async () => {
    get.mockResolvedValueOnce(metrics).mockImplementationOnce(() => new Promise(() => {}));
    const view = render(<CacheUsageMetricsPanel {...props} />, { wrapper: provider() });
    expect(await screen.findByText("150")).toBeInTheDocument();
    view.rerender(<CacheUsageMetricsPanel {...props} userId="other-user" />);
    expect(screen.queryByText("150")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading cache usage for the current filter");
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it.each([{ scope: "self" as const, userId: null }, { endTime: new Date(2026, 1, 2) }, { modelKeys: [] }])(
    "does not issue a wider query for an unavailable filter %o",
    (changes) => {
      render(<CacheUsageMetricsPanel {...props} {...changes} />, { wrapper: provider() });
      expect(screen.getByRole("status")).toHaveTextContent(/unavailable|up to 31 days/);
      expect(get).not.toHaveBeenCalled();
    },
  );

  it("keeps a failed scoped request unavailable without retrying a global request", async () => {
    get.mockRejectedValue(new Error("unsupported scope"));
    render(<CacheUsageMetricsPanel {...props} scope="self" modelDimension="models" modelKeys={["exact/model"]} />, {
      wrapper: provider(),
    });
    expect(await screen.findByText("Cache usage is unavailable for the current filter.")).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("150")).not.toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(
      "/retail/cache-usage",
      expect.objectContaining({
        query: expect.objectContaining({ scope: "self", user_id: "selected-user", models: ["exact/model"] }),
      }),
    );
  });
});
