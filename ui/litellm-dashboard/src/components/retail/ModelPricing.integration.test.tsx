import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { chooseSelectOption, renderWithProviders, testQueryClient } from "../../../tests/test-utils";
import ModelPricingPanel from "./ModelPricingPanel";
import type { CatalogPrice, ModelPriceCatalogEntry } from "./modelPriceCatalog";

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

const builtInPrice: CatalogPrice = {
  ...rate,
  model: "public-one",
  catalog_model: "provider/one",
  source_label: "Loaded reference catalog revision test",
  source: "catalog",
  version: null,
  updated_at: null,
  cache_write_price_published: false,
};

function priceEntry(price: CatalogPrice, status: ModelPriceCatalogEntry["status"]): ModelPriceCatalogEntry {
  return {
    model: price.model,
    providers: ["openai"],
    modes: ["chat"],
    configured: true,
    status,
    price,
    candidates: [price],
    reason: null,
  };
}

function serveApi({
  empty = false,
  cacheWriteTiers,
  fail = false,
  entries,
}: {
  empty?: boolean;
  cacheWriteTiers?: Record<string, string>;
  fail?: boolean;
  entries?: ModelPriceCatalogEntry[];
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
      const saved: CatalogPrice = {
        ...savedPrice,
        source: "legacy",
        catalog_model: null,
        cache_write_tiers: cacheWriteTiers,
        cache_write_price_published: true,
      };
      const data = path.endsWith("/reference-prices")
        ? { items: candidates, unavailable: [{ model: "unpriced-model", reason: "Manual reference required" }] }
        : { items: entries ?? [priceEntry(builtInPrice, "default"), ...(empty ? [] : [priceEntry(saved, "custom")])] };
      return new Response(JSON.stringify(data), { status: 200 });
    }),
  );
  return { writes, requests };
}

function renderPanel(options?: Parameters<typeof renderWithProviders>[1]) {
  return renderWithProviders(<ModelPricingPanel />, options);
}

beforeEach(() => {
  testQueryClient.clear();
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

it("loads defaults and saved overrides without importing or writing prices", async () => {
  const { requests, writes } = serveApi();
  renderPanel();
  expect(screen.getByRole("heading", { name: "Model pricing", level: 1 })).toBeInTheDocument();
  expect(await screen.findByRole("row", { name: /legacy-model/ })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Base prices 2 models" })).toBeInTheDocument();
  expect(screen.getByText(/Site-wide base rates in USD per 1M tokens/)).toHaveTextContent(
    "Customer charges use these rates with the applicable customer, plan, or resource pool discount.",
  );
  expect(screen.getByRole("row", { name: /public-one/ })).toHaveTextContent("Built-in default");
  expect(screen.getByRole("row", { name: /legacy-model/ })).toHaveTextContent("Custom price");
  expect(requests).toEqual(["/retail/admin/pricing-catalog"]);
  expect(writes).toEqual([]);
});

it("shows an editable built-in table even when no overrides have been saved", async () => {
  const { requests, writes } = serveApi({ empty: true });
  renderPanel();
  const row = await screen.findByRole("row", { name: /public-one/ });
  expect(screen.getByText("0 saved overrides · 1 built-in defaults")).toBeInTheDocument();
  expect(within(row).getByRole("button", { name: "Edit price" })).toBeEnabled();
  expect(screen.getByRole("combobox", { name: "Providers" })).toHaveTextContent("All providers");
  expect(screen.getByRole("combobox", { name: "Model types" })).toHaveTextContent("All model types");
  expect(screen.getByRole("combobox", { name: "Price sources" })).toHaveTextContent("All price sources");
  expect(screen.getByRole("button", { name: "Import catalog prices" })).toBeEnabled();
  expect(requests).toEqual(["/retail/admin/pricing-catalog"]);
  expect(writes).toEqual([]);
});

it("reports base price loading failures instead of a zero configured state", async () => {
  serveApi({ fail: true });
  renderPanel();
  expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load base prices");
  expect(screen.getByRole("alert")).toHaveTextContent("Pricing service unavailable");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("requires explicit catalog import and a choice for aliases with multiple references", async () => {
  const { writes } = serveApi();
  renderPanel();
  expect(await screen.findByRole("row", { name: /legacy-model/ })).toBeInTheDocument();
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
        body: {
          ...rate,
          model: savedPrice.model,
          output_per_million: "12",
          cache_write_tiers: cacheWriteTiers,
          cache_write_price_published: true,
        },
      },
    ]),
  );
});

it.each([
  { publish: false, writeRate: rate.cache_write_per_million },
  { publish: true, writeRate: "0" },
])(
  "edits a built-in row directly while preserving explicit cache publication choice $publish",
  async ({ publish, writeRate }) => {
    const { writes } = serveApi({ empty: true });
    renderPanel();
    const row = await screen.findByRole("row", { name: /public-one/ });
    expect(within(row).getByText("Not listed")).toBeInTheDocument();
    fireEvent.click(within(row).getByRole("button", { name: "Edit price" }));
    expect(screen.getByRole("textbox", { name: "Public model name" })).toHaveValue(builtInPrice.model);
    expect(screen.getByRole("textbox", { name: "Input (USD / 1M)" })).toHaveValue(rate.input_per_million);
    fireEvent.change(screen.getByRole("textbox", { name: "Output (USD / 1M)" }), { target: { value: "12" } });
    if (publish) {
      fireEvent.change(screen.getByRole("textbox", { name: "Cache write (default) (USD / 1M)" }), {
        target: { value: writeRate },
      });
    }
    fireEvent.click(screen.getByRole("button", { name: "Save base price" }));
    await waitFor(() =>
      expect(writes).toEqual([
        {
          path: "/retail/admin/model-prices",
          body: {
            ...rate,
            model: builtInPrice.model,
            output_per_million: "12",
            cache_write_per_million: writeRate,
            cache_write_tiers: {},
            cache_write_price_published: publish,
          },
        },
      ]),
    );
  },
);

it("combines provider and type filters with search and can clear them", async () => {
  const embedding = priceEntry({ ...builtInPrice, model: "search-embed" }, "default");
  const anthropic = {
    ...priceEntry({ ...builtInPrice, model: "claude-fixture" }, "default"),
    providers: ["anthropic"],
  };
  const custom = priceEntry({ ...builtInPrice, model: "custom-fixture", source: "manual" }, "custom");
  serveApi({
    entries: [priceEntry(builtInPrice, "default"), { ...embedding, modes: ["embedding"] }, anthropic, custom],
  });
  const onUrlUpdate = vi.fn();
  renderPanel({ searchParams: { unrelated: "keep" }, onUrlUpdate });
  await screen.findByRole("row", { name: /public-one/ });
  const user = userEvent.setup();
  await chooseSelectOption(user, screen.getByRole("combobox", { name: "Providers" }), "Anthropic");
  expect(screen.queryByRole("row", { name: /public-one/ })).not.toBeInTheDocument();
  expect(screen.getByRole("row", { name: /claude-fixture/ })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  await chooseSelectOption(user, screen.getByRole("combobox", { name: "Price sources" }), "Custom price");
  expect(screen.getByRole("row", { name: /custom-fixture/ })).toBeInTheDocument();
  expect(screen.queryByRole("row", { name: /search-embed/ })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Search model prices" }), { target: { value: "missing" } });
  expect(await screen.findByText("No matching model prices")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  await screen.findByRole("row", { name: /public-one/ });
  await chooseSelectOption(user, screen.getByRole("combobox", { name: "Model types" }), "Embeddings");
  expect(screen.getByRole("row", { name: /search-embed/ })).toBeInTheDocument();
  expect(screen.queryByRole("row", { name: /public-one/ })).not.toBeInTheDocument();
  await waitFor(() => expect(onUrlUpdate).toHaveBeenCalled());
  expect(onUrlUpdate.mock.lastCall?.[0].searchParams.get("unrelated")).toBe("keep");
});

it("paginates the catalog and returns to the first page when searching", async () => {
  serveApi({
    entries: Array.from({ length: 26 }, (_, index) =>
      priceEntry({ ...builtInPrice, model: `public-${String(index).padStart(2, "0")}` }, "default"),
    ),
  });
  renderPanel();
  await screen.findByRole("row", { name: /public-00/ });
  expect(screen.queryByRole("row", { name: /public-25/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Next price page" }));
  expect(await screen.findByRole("row", { name: /public-25/ })).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Search model prices" }), { target: { value: "public-00" } });
  expect(await screen.findByRole("row", { name: /public-00/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Previous price page" })).toBeDisabled();
});

it("requires an explicit reference choice before editing an ambiguous model", async () => {
  const first = { ...builtInPrice, model: "ambiguous-model", catalog_model: "provider/first" };
  const second = { ...first, catalog_model: "provider/second", output_per_million: "17" };
  const entry = { ...priceEntry(first, "ambiguous"), price: null, candidates: [first, second] };
  const { writes } = serveApi({ entries: [entry] });
  renderPanel();
  const row = await screen.findByRole("row", { name: /ambiguous-model/ });
  fireEvent.click(within(row).getByRole("button", { name: "Choose reference" }));
  expect(screen.getByRole("button", { name: "Edit selected reference" })).toBeDisabled();
  const user = userEvent.setup();
  await chooseSelectOption(user, screen.getByRole("combobox", { name: "Price reference" }), "provider/second");
  fireEvent.click(screen.getByRole("button", { name: "Edit selected reference" }));
  expect(screen.getByRole("textbox", { name: "Public model name" })).toHaveValue("ambiguous-model");
  expect(screen.getByRole("textbox", { name: "Output (USD / 1M)" })).toHaveValue(second.output_per_million);
  expect(writes).toEqual([]);
});

it("shows internal context tier prices for review and preloads a custom price without copying", async () => {
  const entry: ModelPriceCatalogEntry = {
    ...priceEntry(builtInPrice, "unavailable"),
    price: null,
    candidates: [],
    review_references: [
      {
        catalog_model: "provider/context-tiered",
        source_label: "Loaded configuration fixture",
        rates: { input_cost_per_token: "0.000002", input_cost_per_token_above_200k_tokens: "0.000004" },
        base_price: builtInPrice,
      },
    ],
  };
  const { writes } = serveApi({ entries: [entry] });
  renderPanel();
  const row = await screen.findByRole("row", { name: /public-one/ });
  expect(within(row).getByText("Reference only")).toBeInTheDocument();
  expect(within(row).getByText("Needs review")).toBeInTheDocument();
  fireEvent.click(within(row).getByRole("button", { name: "Review price" }));
  const configuration = screen.getByRole("table", { name: "Internal price configuration" });
  expect(within(configuration).getByRole("row", { name: /input_cost_per_token_above_200k_tokens/ })).toHaveTextContent(
    "0.000004",
  );
  expect(screen.getByText(/Additional context tiers are not copied into a flat price/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Use as custom base price" }));
  expect(screen.getByRole("textbox", { name: "Public model name" })).toHaveValue(builtInPrice.model);
  expect(screen.getByRole("textbox", { name: "Input (USD / 1M)" })).toHaveValue(builtInPrice.input_per_million);
  expect(writes).toEqual([]);
});

it("keeps non-token configuration units visible without converting them into token prices", async () => {
  const entry: ModelPriceCatalogEntry = {
    ...priceEntry({ ...builtInPrice, model: "image-model" }, "unavailable"),
    price: null,
    candidates: [],
    modes: ["image_generation"],
    review_references: [
      {
        catalog_model: "provider/image-model",
        source_label: "Loaded configuration fixture",
        rates: { output_cost_per_image: "0.04" },
        base_price: null,
      },
    ],
  };
  const { writes } = serveApi({ entries: [entry] });
  renderPanel();
  const row = await screen.findByRole("row", { name: /image-model/ });
  expect(within(row).queryByText("$0.04")).not.toBeInTheDocument();
  fireEvent.click(within(row).getByRole("button", { name: "Review price" }));
  expect(screen.getByRole("table", { name: "Internal price configuration" })).toHaveTextContent(
    "output_cost_per_image",
  );
  expect(screen.getByRole("table", { name: "Internal price configuration" })).toHaveTextContent("0.04");
  expect(screen.queryByRole("button", { name: "Use as custom base price" })).not.toBeInTheDocument();
  expect(writes).toEqual([]);
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
