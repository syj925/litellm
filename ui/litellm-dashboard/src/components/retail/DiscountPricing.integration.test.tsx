import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import { afterEach, expect, it, vi } from "vitest";
import AdminWalletsPanel from "./AdminWalletsPanel";
import WalletPanel from "./WalletPanel";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    accessToken: "test-admin",
    userId: "customer-example",
    userRole: "Admin",
    isAuthorized: true,
    isLoading: false,
  }),
}));
vi.mock("@/lib/toast", () => ({ toast: { success: vi.fn() } }));

const plan = { id: "standard", name: "Standard", multiplier: "1", discount: "1" };
const wallet = {
  user_id: "customer-example",
  currency: "USD",
  balance: "10",
  available: "10",
  reserved: "0",
  total_credited: "10",
  total_spent: "0",
  pending_requests: 0,
  pricing_plan: plan,
  discount: "1",
  discount_override: null,
  discount_source: "plan",
};
const modelDiscounts = [
  {
    model: "alpha-model",
    discount: "0.65",
    discount_source: "customer_model",
    customer_discount: "0.65",
    plan_discount: "0.8",
  },
  {
    model: "beta-model",
    discount: "0.8",
    discount_source: "plan_model",
    customer_discount: null,
    plan_discount: "0.8",
  },
  { model: "gamma-model", discount: "1", discount_source: "standard", customer_discount: null, plan_discount: null },
];
const rate = {
  input_per_million: "2",
  output_per_million: "10",
  cache_read_per_million: "0.2",
  cache_write_per_million: "2.5",
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

function serveApi(activeModels = modelDiscounts.map((row) => row.model)) {
  const writes: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const path = new URL(input, "http://localhost").pathname;
      if (options?.method && options.method !== "GET") {
        const body: unknown = JSON.parse(String(options.body));
        writes.push({ path, body });
        return new Response(JSON.stringify(path.endsWith("import") ? { items: [] } : wallet), { status: 200 });
      }
      const data = path.endsWith("/model-discounts")
        ? {
            user_id: wallet.user_id,
            plan_id: plan.id,
            items: path.includes("/admin/plans/")
              ? modelDiscounts.map((row) => ({
                  ...row,
                  discount: row.plan_discount ?? "1",
                  discount_source: row.plan_discount ? "plan_model" : "standard",
                }))
              : modelDiscounts,
          }
        : path.endsWith("/discount-models")
          ? { models: activeModels }
          : path.endsWith("/plans")
            ? { items: [plan] }
            : path.endsWith("/wallets")
              ? { items: [wallet], total: 1 }
              : path.endsWith("/wallet")
                ? wallet
                : path.endsWith("/model-prices")
                  ? {
                      items: [
                        {
                          ...rate,
                          model: "legacy-model",
                          version: 1,
                          updated_at: "2026-01-01T00:00:00Z",
                          source: "legacy",
                          source_label: "Existing unverified base price",
                        },
                      ],
                    }
                  : path.endsWith("/reference-prices")
                    ? {
                        items: candidates,
                        unavailable: [{ model: "unpriced-model", reason: "Manual reference required" }],
                      }
                    : { items: [], total: 0 };
      return new Response(JSON.stringify(data), { status: 200 });
    }),
  );
  return writes;
}

function renderPanel(panel = <AdminWalletsPanel />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <NuqsTestingAdapter>
      <QueryClientProvider client={client}>{panel}</QueryClientProvider>
    </NuqsTestingAdapter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

it("creates a shared model pricing table without an account-wide discount", async () => {
  const writes = serveApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "New plan" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Plan ID" }), { target: { value: "contract" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Plan name" }), { target: { value: "Contract" } });
  expect(screen.queryByRole("textbox", { name: "Pay percentage of reference price (%)" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
  await waitFor(() =>
    expect(writes).toEqual([{ path: "/retail/admin/plans", body: { id: "contract", name: "Contract" } }]),
  );
});

it("applies a precise customer discount only to explicitly selected models and can remove one override", async () => {
  const writes = serveApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Model discounts for customer customer-example" }));
  expect(await screen.findByRole("checkbox", { name: "Select model alpha-model" })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Apply to 0 models" })).toBeDisabled();
  const user = userEvent.setup();
  fireEvent.change(screen.getByRole("textbox", { name: "Search model discounts" }), { target: { value: "alpha" } });
  fireEvent.click(screen.getByRole("button", { name: "Select visible models" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Search model discounts" }), { target: { value: "" } });
  await user.click(screen.getByRole("checkbox", { name: "Select model beta-model" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Pay percentage of reference price (%)" }), {
    target: { value: "65.1250001" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Apply to 2 models" }));
  await waitFor(() =>
    expect(writes[0]).toEqual({
      path: "/retail/admin/wallets/customer-example/model-discounts",
      body: { models: ["alpha-model", "beta-model"], discount: "0.651250001" },
    }),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "Apply to 0 models" })).toBeDisabled());
  await user.click(screen.getByRole("checkbox", { name: "Select model alpha-model" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove selected overrides" }));
  await waitFor(() =>
    expect(writes[1]).toEqual({
      path: "/retail/admin/wallets/customer-example/model-discounts",
      body: { models: ["alpha-model"], discount: null },
    }),
  );
});

it("can remove an inactive model's existing customer override without applying a new discount", async () => {
  const writes = serveApi(["beta-model", "gamma-model"]);
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Model discounts for customer customer-example" }));
  const user = userEvent.setup();
  await user.click(await screen.findByRole("checkbox", { name: "Select model alpha-model" }));
  expect(screen.getByRole("button", { name: "Apply to 1 models" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Remove selected overrides" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Remove selected overrides" }));
  await waitFor(() =>
    expect(writes).toEqual([
      {
        path: "/retail/admin/wallets/customer-example/model-discounts",
        body: { models: ["alpha-model"], discount: null },
      },
    ]),
  );
});

it("shares selected model rules through a plan and rejects a markup", async () => {
  const writes = serveApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Model discounts for plan Standard" }));
  const user = userEvent.setup();
  await user.click(await screen.findByRole("checkbox", { name: "Select model beta-model" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Pay percentage of reference price (%)" }), {
    target: { value: "101" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Apply to 1 models" }));
  expect(screen.getByRole("alert")).toHaveTextContent("at most 100");
  expect(writes).toEqual([]);
  fireEvent.change(screen.getByRole("textbox", { name: "Pay percentage of reference price (%)" }), {
    target: { value: "70" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Apply to 1 models" }));
  await waitFor(() =>
    expect(writes).toEqual([
      { path: "/retail/admin/plans/standard/model-discounts", body: { models: ["beta-model"], discount: "0.7" } },
    ]),
  );
});

it("requires explicit catalog import and a choice for aliases with multiple references", async () => {
  const writes = serveApi();
  renderPanel();
  expect(await screen.findByText("Legacy, unverified")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Import catalog references" }));
  expect(await screen.findByRole("checkbox", { name: "Import reference for public-two" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(writes).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Select single-match models" }));
  expect(screen.getByRole("button", { name: "Apply 1 reference prices" })).toBeEnabled();
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Catalog reference for public-two" }));
  await user.click(await screen.findByRole("option", { name: "provider/two-region-b" }));
  fireEvent.click(await screen.findByRole("button", { name: "Apply 2 reference prices" }));
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

it("shows different effective discounts for each customer model without an account-wide percentage", async () => {
  serveApi();
  renderPanel(<WalletPanel />);
  const account = await screen.findByText("65% of reference price (35% off)");
  expect(account).toBeInTheDocument();
  expect(screen.getByText("Customer model override")).toBeInTheDocument();
  expect(screen.getByText("80% of reference price (20% off)")).toBeInTheDocument();
  expect(screen.getByText("100% of reference price (0% off)")).toBeInTheDocument();
  expect(screen.getByText("Standard")).toBeInTheDocument();
});
