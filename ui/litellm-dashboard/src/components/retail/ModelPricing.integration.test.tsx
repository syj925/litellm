import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ModelPricingPanel from "./ModelPricingPanel";

const { auth, authorizedAdmin } = vi.hoisted(() => {
  const authorizedAdmin = {
    accessToken: "test-admin" as string | null,
    userRole: "Admin",
    isAuthorized: true,
    isLoading: false,
  };
  return { auth: { ...authorizedAdmin }, authorizedAdmin };
});

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => auth }));
vi.mock("@/lib/toast", () => ({ toast: { success: vi.fn() } }));

const rate = {
  input_per_million: "2",
  output_per_million: "10",
  cache_read_per_million: "0.2",
  cache_write_per_million: "2.5",
};
const savedPrice = {
  ...rate,
  model: "legacy-model",
  version: 1,
  updated_at: "2026-01-01T00:00:00Z",
  source: "legacy",
  source_label: "Existing unverified base price",
};
const candidates = [
  { ...rate, model: "public-one", catalog_model: "provider/one", source_label: "Reference catalog revision test" },
  {
    ...rate,
    model: "public-two",
    catalog_model: "provider/two-region-a",
    source_label: "Reference catalog revision test",
  },
  {
    ...rate,
    model: "public-two",
    catalog_model: "provider/two-region-b",
    source_label: "Reference catalog revision test",
  },
];

function serveApi({
  empty = false,
  cacheWriteTiers,
  fail = false,
}: {
  empty?: boolean;
  cacheWriteTiers?: Record<string, string>;
  fail?: boolean;
} = {}) {
  const writes: { path: string; body: unknown }[] = [];
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const path = new URL(input, "http://localhost").pathname;
      requests.push(path);
      if (options?.method && options.method !== "GET") {
        writes.push({ path, body: JSON.parse(String(options.body)) });
        return new Response(JSON.stringify(path.endsWith("import") ? { items: [] } : savedPrice), { status: 200 });
      }
      if (fail) return new Response(JSON.stringify({ detail: "Pricing service unavailable" }), { status: 503 });
      const data = path.endsWith("/reference-prices")
        ? { items: candidates, unavailable: [{ model: "unpriced-model", reason: "Manual reference required" }] }
        : { items: empty ? [] : [{ ...savedPrice, cache_write_tiers: cacheWriteTiers }] };
      return new Response(JSON.stringify(data), { status: 200 });
    }),
  );
  return { writes, requests };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ModelPricingPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.assign(auth, authorizedAdmin);
});
afterEach(() => vi.unstubAllGlobals());

it.each([
  { state: { isLoading: true }, title: "Loading model pricing" },
  { state: { isAuthorized: false }, title: "Model pricing is unavailable" },
  { state: { accessToken: null }, title: "Model pricing is unavailable" },
  { state: { userRole: "Internal User" }, title: "Administrator access required" },
])("guards model pricing access with $title", ({ state, title }) => {
  Object.assign(auth, state);
  const { requests } = serveApi();
  renderPanel();
  expect(screen.getByText(title)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Add base price" })).not.toBeInTheDocument();
  expect(requests).toEqual([]);
});

it("shows configured base prices as the primary admin editor without loading or importing catalog defaults", async () => {
  const { requests, writes } = serveApi();
  renderPanel();
  expect(screen.getByRole("heading", { name: "Model pricing", level: 1 })).toBeInTheDocument();
  expect(await screen.findByRole("row", { name: /legacy-model/ })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Base prices 1 configured" })).toBeInTheDocument();
  expect(screen.getByText(/Site-wide base rates in USD per 1M tokens/)).toHaveTextContent(
    "Customer charges use these rates with the applicable customer, plan, or resource pool discount.",
  );
  expect(screen.getByText("Legacy, unverified")).toBeInTheDocument();
  expect(requests).toEqual(["/retail/admin/model-prices"]);
  expect(writes).toEqual([]);
});

it("explains that zero saved base prices still allows catalog default pricing", async () => {
  const { requests, writes } = serveApi({ empty: true });
  renderPanel();
  expect(await screen.findByText("No custom base prices configured")).toBeInTheDocument();
  expect(screen.getByText(/Models can use catalog defaults without a saved base price here/)).toHaveTextContent(
    "Import catalog rates to review and customize them",
  );
  expect(screen.getByRole("heading", { name: "Base prices 0 configured" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Import catalog prices" })).toBeEnabled();
  expect(requests).toEqual(["/retail/admin/model-prices"]);
  expect(writes).toEqual([]);
});

it("reports base price loading failures instead of a zero configured state", async () => {
  serveApi({ fail: true });
  renderPanel();
  expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load base prices");
  expect(screen.getByRole("alert")).toHaveTextContent("Pricing service unavailable");
  expect(screen.queryByText("No custom base prices configured")).not.toBeInTheDocument();
});

it("requires explicit catalog import and a choice for aliases with multiple references", async () => {
  const { writes } = serveApi();
  renderPanel();
  expect(await screen.findByText("Legacy, unverified")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Import catalog prices" }));
  expect(await screen.findByRole("checkbox", { name: "Import base price for public-two" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Select single-match models" }));
  expect(screen.getByRole("button", { name: "Apply 1 base prices" })).toBeEnabled();
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Catalog reference for public-two" }));
  await user.click(await screen.findByRole("option", { name: "provider/two-region-b" }));
  fireEvent.click(await screen.findByRole("button", { name: "Apply 2 base prices" }));
  await waitFor(() =>
    expect(writes).toEqual([
      {
        path: "/retail/admin/reference-prices/import",
        body: {
          items: [
            { model: "public-one", catalog_model: "provider/one", source_label: "Reference catalog revision test" },
            {
              model: "public-two",
              catalog_model: "provider/two-region-b",
              source_label: "Reference catalog revision test",
            },
          ],
        },
      },
    ]),
  );
});

it("shows saved duration prices and preserves every duration when editing an existing base price", async () => {
  const cacheWriteTiers = { "300": rate.cache_write_per_million, "3600": "4" };
  const { writes } = serveApi({ cacheWriteTiers });
  renderPanel();
  const saved = await screen.findByRole("row", { name: /legacy-model/ });
  expect(within(saved).getByText("Cache write · 5m")).toBeInTheDocument();
  expect(within(saved).getByText("Cache write · 1h")).toBeInTheDocument();
  expect(within(saved).getAllByText("$2.50")).toHaveLength(1);
  expect(within(saved).queryByText("Cache write", { exact: true })).not.toBeInTheDocument();
  fireEvent.click(within(saved).getByRole("button", { name: "Edit price" }));
  expect(screen.getByRole("textbox", { name: "5m price (USD / 1M)" })).toHaveValue(cacheWriteTiers["300"]);
  expect(screen.getByRole("textbox", { name: "1h price (USD / 1M)" })).toHaveValue(cacheWriteTiers["3600"]);
  fireEvent.change(screen.getByRole("textbox", { name: "Output (USD / 1M)" }), { target: { value: "12" } });
  fireEvent.click(screen.getByRole("button", { name: "Save base price" }));
  await waitFor(() =>
    expect(writes).toEqual([
      {
        path: "/retail/admin/model-prices",
        body: { ...rate, model: savedPrice.model, output_per_million: "12", cache_write_tiers: cacheWriteTiers },
      },
    ]),
  );
});

it("saves independent cache duration rates and rejects duplicate durations without a write", async () => {
  const { writes } = serveApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Add base price" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Public model name" }), { target: { value: "cache-model" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Input (USD / 1M)" }), {
    target: { value: rate.input_per_million },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Output (USD / 1M)" }), {
    target: { value: rate.output_per_million },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Cache read (USD / 1M)" }), {
    target: { value: rate.cache_read_per_million },
  });
  fireEvent.click(screen.getByRole("button", { name: "+ 5m" }));
  fireEvent.click(screen.getByRole("button", { name: "+ 1h" }));
  fireEvent.change(screen.getByRole("textbox", { name: "5m price (USD / 1M)" }), { target: { value: "2.5" } });
  fireEvent.change(screen.getByRole("textbox", { name: "1h price (USD / 1M)" }), { target: { value: "4" } });
  fireEvent.click(screen.getByRole("button", { name: "Add duration" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Duration 3 (seconds)" }), { target: { value: "300" } });
  fireEvent.change(screen.getAllByRole("textbox", { name: "5m price (USD / 1M)" })[1], { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "Save base price" }));
  expect(screen.getByRole("alert")).toHaveTextContent("only one price");
  expect(writes).toEqual([]);
  fireEvent.change(screen.getByRole("textbox", { name: "Duration 3 (seconds)" }), { target: { value: "75" } });
  fireEvent.click(screen.getByRole("button", { name: "Save base price" }));
  await waitFor(() =>
    expect(writes).toEqual([
      {
        path: "/retail/admin/model-prices",
        body: {
          ...rate,
          model: "cache-model",
          cache_write_per_million: "0",
          cache_write_tiers: { "300": "2.5", "3600": "4", "75": "0" },
        },
      },
    ]),
  );
});
