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

const plan = { id: "standard", name: "Standard", multiplier: "0.8", discount: "0.8" };
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
  discount: "0.65",
  discount_override: "0.65",
  discount_source: "customer",
};
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

function serveApi() {
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
      const data = path.endsWith("/plans")
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
                ? { items: candidates, unavailable: [{ model: "unpriced-model", reason: "Manual reference required" }] }
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

it("sends a percentage-paid plan as an exact discount, without changing model rates", async () => {
  const writes = serveApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "New plan" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Plan ID" }), { target: { value: "contract" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Plan name" }), { target: { value: "Contract" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Pay percentage of reference price (%)" }), {
    target: { value: "70.1234567" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
  await waitFor(() =>
    expect(writes).toEqual([
      { path: "/retail/admin/plans", body: { id: "contract", name: "Contract", discount: "0.701234567" } },
    ]),
  );
});

it("saves a customer override and can explicitly restore shared-plan inheritance", async () => {
  const writes = serveApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Set discount" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Pay percentage of reference price (%)" }), {
    target: { value: "65.1250001" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save customer discount" }));
  await waitFor(() =>
    expect(writes[0]).toEqual({
      path: "/retail/admin/wallets/customer-example/discount",
      body: { discount: "0.651250001" },
    }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "Set discount" }));
  fireEvent.click(screen.getByRole("button", { name: "Use plan discount" }));
  await waitFor(() =>
    expect(writes[1]).toEqual({ path: "/retail/admin/wallets/customer-example/discount", body: { discount: null } }),
  );
});

it("rejects a markup when editing an ordinary discount plan", async () => {
  const writes = serveApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Edit plan" }));
  expect(screen.getByRole("textbox", { name: "Plan ID" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "Pay percentage of reference price (%)" }), {
    target: { value: "101" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
  expect(screen.getByRole("alert")).toHaveTextContent("at most 100");
  expect(writes).toEqual([]);
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

it("shows a customer's effective override separately from the shared plan", async () => {
  serveApi();
  renderPanel(<WalletPanel />);
  const account = await screen.findByText("65% of reference price (35% off)");
  expect(account).toBeInTheDocument();
  expect(screen.getByText("Customer override")).toBeInTheDocument();
  expect(screen.getByText("Standard")).toBeInTheDocument();
});
