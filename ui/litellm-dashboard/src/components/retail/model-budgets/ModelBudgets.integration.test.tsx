import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import AdminWalletsPanel from "../AdminWalletsPanel";
import WalletPanel from "../WalletPanel";
import { beijingTimeInput } from "../management/time";
import AdminModelBudgetsDialog from "./AdminModelBudgetsDialog";
import { BudgetScopeNote } from "./BudgetShared";
import type { AdminModelBudget, CreateModelBudget, ScheduleModelBudget, UpdateModelBudget } from "./types";

const auth = vi.hoisted(() => ({ userRole: "Admin", isViewOnly: false }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ ...auth, accessToken: "test-session", userId: "actor-one", isAuthorized: true, isLoading: false }),
}));

const plan = { id: "standard", name: "Standard", multiplier: "1", discount: "1" };
const wallet = {
  user_id: "customer-example",
  currency: "USD",
  balance: "100",
  available: "90",
  reserved: "10",
  total_credited: "120",
  total_spent: "20",
  pending_requests: 1,
  pricing_plan: plan,
};
const budget: AdminModelBudget = {
  id: "f4849375-33a6-4bd6-b87b-efaf87fc0046",
  user_id: wallet.user_id,
  model: "public/alpha",
  effective_from: "2026-11-04T00:00:00Z",
  ends_at: "2026-12-04T00:00:00Z",
  limit_usd: "30.000000000",
  spent_usd: "5.000000000",
  held_usd: "2.000000000",
  remaining_usd: "23.000000000",
  overrun_usd: "0.000000000",
  enabled: true,
  config_version: 1,
  accounting_version: 2,
  accounted_at: "2026-11-04T01:00:00Z",
  predecessor_id: null,
  successor_id: null,
  state: "active",
  source: "management_api",
  created_at: "2026-11-04T00:00:00Z",
  created_by: "private-operator",
  updated_at: "2026-11-04T00:00:00Z",
  updated_by: "private-operator",
};
type Call = { url: URL; method: string; body: unknown; headers: Headers };
type State = {
  enabled: boolean;
  rows: AdminModelBudget[];
  writeStatus: number;
  listStatus: number;
  modelsStatus: number;
  modelTotal: number;
  budgetTotal: number;
};
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function page<T>(items: T[], total = items.length, offset = 0) {
  return { items, total, limit: 25, offset };
}
function serveModels(call: Call, state: State) {
  if (state.modelsStatus !== 200) return json({ detail: "Model authority unavailable" }, state.modelsStatus);
  const offset = Number(call.url.searchParams.get("offset"));
  const models = offset > 0 ? [{ model: "public/beta" }] : [{ model: "public/alpha" }, { model: "public/beta" }];
  return json(page(models, state.modelTotal, offset));
}
function updatedState(row: AdminModelBudget, enabled: boolean): AdminModelBudget["state"] {
  if (row.state === "scheduled") return "scheduled";
  return enabled ? "active" : "disabled";
}
function serveMutation(call: Call, state: State) {
  if (state.writeStatus !== 200)
    return json(
      { detail: state.writeStatus === 409 ? "Budget version conflict" : "Budget save unavailable" },
      state.writeStatus,
    );
  if (call.method === "POST") {
    if (call.url.pathname.endsWith("/successor")) {
      const payload = call.body as ScheduleModelBudget;
      const predecessorId = call.url.pathname.split("/").at(-2);
      const predecessor = state.rows.find((row) => row.id === predecessorId);
      if (!predecessor) return json({ detail: "Budget unavailable" }, 404);
      const created: AdminModelBudget = {
        ...budget,
        id: "8ac98597-89fd-40e0-830a-412e129a1ed7",
        model: predecessor.model,
        effective_from: predecessor.ends_at,
        ends_at: payload.ends_at,
        limit_usd: payload.limit_usd,
        remaining_usd: payload.limit_usd,
        spent_usd: "0.000000000",
        held_usd: "0.000000000",
        predecessor_id: predecessor.id,
        enabled: payload.enabled,
        state: "scheduled",
      };
      state.rows = [
        ...state.rows.map((row) => (row.id === predecessor.id ? { ...row, successor_id: created.id } : row)),
        created,
      ];
      state.budgetTotal += 1;
      return json(created, 201);
    }
    const payload = call.body as CreateModelBudget;
    const created: AdminModelBudget = {
      ...budget,
      id: "8ac98597-89fd-40e0-830a-412e129a1ed7",
      model: payload.model,
      ends_at: payload.ends_at,
      limit_usd: payload.limit_usd,
      remaining_usd: payload.limit_usd,
      spent_usd: "0.000000000",
      held_usd: "0.000000000",
      enabled: payload.enabled,
      state: payload.enabled ? "active" : "disabled",
    };
    state.rows = [...state.rows, created];
    return json(created, 201);
  }
  const payload = call.body as UpdateModelBudget;
  const target = call.url.pathname.split("/").at(-1);
  state.rows = state.rows.map((row) =>
    row.id === target
      ? {
          ...row,
          limit_usd: payload.limit_usd,
          enabled: payload.enabled,
          config_version: payload.expected_version + 1,
          state: updatedState(row, payload.enabled),
        }
      : row,
  );
  return json(state.rows.find((row) => row.id === target));
}
function serve() {
  const calls: Call[] = [];
  const state: State = {
    enabled: true,
    rows: [budget],
    writeStatus: 200,
    listStatus: 200,
    modelsStatus: 200,
    modelTotal: 2,
    budgetTotal: 1,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const call = {
        url: new URL(input),
        method: options?.method ?? "GET",
        body: options?.body ? (JSON.parse(String(options.body)) as unknown) : null,
        headers: new Headers(options?.headers),
      };
      calls.push(call);
      const path = call.url.pathname;
      if (path === "/retail/config") return json({ model_budgets_enabled: state.enabled });
      if (path === "/retail/admin/wallets") return json(page([wallet]));
      if (path === "/retail/plans") return json({ items: [plan] });
      if (path === "/retail/wallet") return json({ ...wallet, user_id: "actor-one" });
      if (path === "/retail/pools") return json({ items: [], routing_enabled: false });
      if (path === "/retail/model-discounts") return json({ user_id: "actor-one", plan_id: plan.id, items: [] });
      if (path.endsWith("/model-budgets/models")) return serveModels(call, state);
      if (path.includes("/model-budgets") && call.method === "GET") {
        if (state.listStatus !== 200) return json({ detail: "Budget authority unavailable" }, state.listStatus);
        return json(page(state.rows, state.budgetTotal));
      }
      if (path.includes("/model-budgets") && (call.method === "POST" || call.method === "PATCH")) {
        return serveMutation(call, state);
      }
      return json(page([]));
    }),
  );
  return { calls, state };
}
beforeEach(() => {
  auth.userRole = "Admin";
  auth.isViewOnly = false;
  testQueryClient.clear();
});
afterEach(() => {
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

async function openAdmin() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Model budgets for customer customer-example" }));
  await screen.findByRole("button", { name: "New model budget" });
  return user;
}
function fillEnd() {
  const future = new Date(Date.now() + 2 * 24 * 60 * 60 * 1000);
  future.setUTCSeconds(0, 0);
  fireEvent.change(screen.getByLabelText("End time (Beijing)"), {
    target: { value: beijingTimeInput(future.toISOString()) },
  });
  return future.toISOString();
}

function fillSuccessorEnd() {
  const end = new Date(Date.parse(budget.ends_at) + 2 * 24 * 60 * 60 * 1000).toISOString();
  fireEvent.change(screen.getByLabelText("End time (Beijing)"), {
    target: { value: beijingTimeInput(end) },
  });
  return end;
}

it("keeps budget guidance compact and reveals the full accounting rules on request", async () => {
  renderWithProviders(<BudgetScopeNote />);
  expect(screen.getByText(/Each exact public model shares its budget/)).toBeVisible();
  const accounting = screen.getByText(/Final charges can exceed the estimate/);
  const renewal = screen.getByText(/Windows created manually do not repeat automatically/);
  expect(accounting).not.toBeVisible();
  expect(renewal).not.toBeVisible();
  const user = userEvent.setup();
  await user.click(screen.getByText("How budget windows work"));
  expect(accounting).toBeVisible();
  expect(renewal).toBeVisible();
  await user.click(screen.getByText("How budget windows work"));
  expect(accounting).not.toBeVisible();
});

it("schedules one separate next window with a server-fixed start and the predecessor configuration version", async () => {
  const { calls, state } = serve();
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Schedule next window for public/alpha" }));
  expect(screen.getByText("Next window starts (Beijing)")).toBeInTheDocument();
  expect(screen.queryByLabelText("Public model")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Budget limit (USD)")).toHaveValue(budget.limit_usd);
  expect(screen.getByText(/This does not enable automatic renewal/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Budget limit (USD)"), { target: { value: "40.123456789" } });
  const end = fillSuccessorEnd();
  await user.click(screen.getByRole("button", { name: "Schedule next window", exact: true }));
  await screen.findByText("Scheduled");
  const write = calls.find((call) => call.method === "POST");
  expect(write?.url.pathname).toBe(`/retail/admin/users/customer-example/model-budgets/${budget.id}/successor`);
  const expected = { ends_at: end, limit_usd: "40.123456789", enabled: true, expected_version: 1 };
  expect(write?.body).toEqual(expected);
  expect(write?.headers.get("Idempotency-Key")).toBeTruthy();
  expect(state.rows[0]).toEqual({ ...budget, successor_id: state.rows[1].id });
  expect(screen.getByText("Next window scheduled")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Schedule next window for public/alpha" })).toHaveLength(2);
  for (const button of screen.getAllByRole("button", { name: "Schedule next window for public/alpha" })) {
    expect(button).toBeDisabled();
  }
});

it.each([
  { state: "scheduled" as const, successor_id: null },
  { state: "expired" as const, successor_id: null },
  { state: "active" as const, successor_id: "already-scheduled" },
])("does not schedule another window from a $state budget with successor=$successor_id", async (condition) => {
  const { state, calls } = serve();
  state.rows = [{ ...budget, ...condition }];
  renderWithProviders(<AdminWalletsPanel />);
  await openAdmin();
  expect(screen.getByRole("button", { name: "Schedule next window for public/alpha" })).toBeDisabled();
  expect(calls.some((call) => call.method === "POST")).toBe(false);
});

it("allows a next window after a paused current window and distinguishes its scheduled paused state", async () => {
  const { state, calls } = serve();
  state.rows = [{ ...budget, enabled: false, state: "disabled" }];
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Schedule next window for public/alpha" }));
  expect(screen.getByRole("checkbox", { name: "Enforce this budget" })).not.toBeChecked();
  fillSuccessorEnd();
  await user.click(screen.getByRole("button", { name: "Schedule next window", exact: true }));
  await screen.findByText("Scheduled");
  const futureRow = within(screen.getByRole("row", { name: /Scheduled/ }));
  expect(futureRow.getByText("Enforcement paused")).toBeInTheDocument();
  expect(futureRow.getByRole("button", { name: "Edit budget for public/alpha" })).toBeEnabled();
  expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({ enabled: false, expected_version: 1 });
  await user.click(futureRow.getByRole("button", { name: "Edit budget for public/alpha" }));
  expect(screen.queryByLabelText("End time (Beijing)")).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "Enforce this budget" }));
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  await screen.findByText("Scheduled");
  expect(
    within(screen.getByRole("row", { name: /Scheduled/ })).queryByText("Enforcement paused"),
  ).not.toBeInTheDocument();
  expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({
    limit_usd: budget.limit_usd,
    enabled: true,
    expected_version: 1,
  });
});

it("retries an uncertain successor write with the same idempotency key and does not resubmit a conflict", async () => {
  const { state, calls } = serve();
  state.writeStatus = 503;
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Schedule next window for public/alpha" }));
  fillSuccessorEnd();
  await user.click(screen.getByRole("button", { name: "Schedule next window", exact: true }));
  await screen.findByText("Unable to save model budget");
  state.writeStatus = 409;
  await user.click(screen.getByRole("button", { name: "Schedule next window", exact: true }));
  await screen.findByText("Budget needs review");
  expect(screen.getByRole("button", { name: "Schedule next window", exact: true })).toBeDisabled();
  const writes = calls.filter((call) => call.method === "POST");
  expect(writes).toHaveLength(2);
  expect(writes[1].headers.get("Idempotency-Key")).toBe(writes[0].headers.get("Idempotency-Key"));
  expect(writes[1].body).toEqual(writes[0].body);
  state.rows = [{ ...budget, successor_id: "created-elsewhere" }];
  await user.click(screen.getByRole("button", { name: "Reload budgets" }));
  expect(await screen.findByRole("button", { name: "Schedule next window for public/alpha" })).toBeDisabled();
});

it("shows a personal scheduled paused window without offering schedule or edit controls", async () => {
  auth.userRole = "Internal User";
  const { state, calls } = serve();
  state.rows = [{ ...budget, enabled: false, state: "scheduled", predecessor_id: "previous-window" }];
  renderWithProviders(<WalletPanel />);
  const section = await screen.findByRole("region", { name: "Your model budgets" });
  await within(section).findByText("Scheduled");
  expect(within(section).getByText("Enforcement paused")).toBeInTheDocument();
  expect(within(section).queryByRole("button", { name: /Schedule next window/ })).not.toBeInTheDocument();
  expect(within(section).queryByRole("button", { name: /Edit budget/ })).not.toBeInTheDocument();
  expect(screen.queryByText("private-operator")).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
});

it("opens the account model budget flow and creates a server-started fixed window with exact USD precision", async () => {
  const { state, calls } = serve();
  state.rows = [];
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "New model budget" }));
  await screen.findByRole("option", { name: "public/alpha" });
  fireEvent.change(screen.getByLabelText("Public model"), { target: { value: "public/alpha" } });
  fireEvent.change(screen.getByLabelText("Budget limit (USD)"), { target: { value: "1234567890.123456789" } });
  const endsAt = fillEnd();
  await user.click(screen.getByRole("button", { name: "Create budget" }));
  expect(await screen.findByRole("button", { name: "Edit budget for public/alpha" })).toBeInTheDocument();
  const write = calls.find((call) => call.method === "POST");
  expect(write?.url.pathname).toBe("/retail/admin/users/customer-example/model-budgets");
  const expected = { model: "public/alpha", ends_at: endsAt, limit_usd: "1234567890.123456789", enabled: true };
  expect(write?.body).toEqual(expected);
  expect(write?.headers.get("Idempotency-Key")).toBeTruthy();
  expect(
    calls.some(
      (call) => call.url.pathname.startsWith("/budget/") || call.url.pathname.includes("/management/v1/budgets"),
    ),
  ).toBe(false);
});

it.each([
  { role: "Internal User", viewer: false },
  { role: "Admin", viewer: true },
  { role: "proxy_admin_viewer", viewer: false },
])(
  "does not request any administrator data from the wallet manager for $role viewer=$viewer",
  async ({ role, viewer }) => {
    auth.userRole = role;
    auth.isViewOnly = viewer;
    const { calls } = serve();
    renderWithProviders(<AdminWalletsPanel />);
    expect(await screen.findByText("Administrator access required")).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  },
);

it("keeps personal budgets read-only and does not render management provenance", async () => {
  auth.userRole = "Internal User";
  const { calls, state } = serve();
  state.rows = [{ ...budget, spent_usd: "31.000000000", remaining_usd: "0.000000000", overrun_usd: "3.000000000" }];
  renderWithProviders(<WalletPanel />, { searchParams: { user_id: "foreign-account" } });
  const section = await screen.findByRole("region", { name: "Your model budgets" });
  expect(await within(section).findByText("public/alpha")).toBeInTheDocument();
  expect(within(section).getByText("Above limit: $3.00")).toBeInTheDocument();
  expect(within(section).queryByRole("button", { name: /Edit budget/ })).not.toBeInTheDocument();
  expect(screen.queryByText("private-operator")).not.toBeInTheDocument();
  expect(screen.queryByText("Management API")).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
  const own = calls.find((call) => call.url.pathname === "/retail/model-budgets");
  expect(own?.url.searchParams.has("user_id")).toBe(false);
});

it("hides both budget entry points when the feature is disabled", async () => {
  const { state, calls } = serve();
  state.enabled = false;
  const view = renderWithProviders(<AdminWalletsPanel />);
  expect(
    await screen.findByRole("button", { name: "Model discounts for customer customer-example" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Model budgets for customer customer-example" })).not.toBeInTheDocument();
  view.unmount();
  renderWithProviders(<WalletPanel />);
  await screen.findByRole("heading", { name: "Wallet" });
  expect(screen.queryByRole("region", { name: "Your model budgets" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("model-budgets"))).toBe(false);
});

it("keeps a failed write retry on the same idempotency key and immutable window", async () => {
  const { state, calls } = serve();
  state.writeStatus = 503;
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Edit budget for public/alpha" }));
  fireEvent.change(screen.getByLabelText("Budget limit (USD)"), { target: { value: "0.000000001" } });
  expect(screen.queryByLabelText("End time (Beijing)")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  expect(await screen.findByText("Unable to save model budget")).toBeInTheDocument();
  state.writeStatus = 200;
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  expect(await screen.findByRole("button", { name: "New model budget" })).toBeInTheDocument();
  const writes = calls.filter((call) => call.method === "PATCH");
  expect(writes).toHaveLength(2);
  expect(writes[0].body).toEqual({ limit_usd: "0.000000001", enabled: true, expected_version: 1 });
  expect(writes[1].body).toEqual(writes[0].body);
  expect(writes[1].headers.get("Idempotency-Key")).toBe(writes[0].headers.get("Idempotency-Key"));
});

it("requires current records to be reloaded after a CAS conflict", async () => {
  const { state, calls } = serve();
  state.writeStatus = 409;
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Edit budget for public/alpha" }));
  fireEvent.change(screen.getByLabelText("Budget limit (USD)"), { target: { value: "25" } });
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  expect(await screen.findByText("Budget needs review")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save budget" })).toBeDisabled();
  state.rows = [{ ...budget, config_version: 3, accounting_version: 80 }];
  state.writeStatus = 200;
  await user.click(screen.getByRole("button", { name: "Reload budgets" }));
  await user.click(await screen.findByRole("button", { name: "Edit budget for public/alpha" }));
  fireEvent.change(screen.getByLabelText("Budget limit (USD)"), { target: { value: "26" } });
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(2));
  expect(calls.filter((call) => call.method === "PATCH").at(-1)?.body).toEqual({
    limit_usd: "26",
    enabled: true,
    expected_version: 3,
  });
});

it("does not overwrite a draft or use the accounting version for configuration CAS", async () => {
  const { state, calls } = serve();
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Edit budget for public/alpha" }));
  fireEvent.change(screen.getByLabelText("Budget limit (USD)"), { target: { value: "24" } });
  state.rows = [{ ...budget, accounting_version: 99, spent_usd: "8.000000000" }];
  await testQueryClient.invalidateQueries({ queryKey: ["retail", "model-budgets"] });
  expect(screen.getByLabelText("Budget limit (USD)")).toHaveValue("24");
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(1));
  expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({
    limit_usd: "24",
    enabled: true,
    expected_version: 1,
  });
});

it("removes private budget controls after a permission denial", async () => {
  const { state } = serve();
  state.writeStatus = 403;
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Edit budget for public/alpha" }));
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  expect(await screen.findByText("Budget access changed")).toBeInTheDocument();
  expect(screen.queryByText("private-operator")).not.toBeInTheDocument();
  expect(screen.queryByText("public/alpha")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New model budget" })).not.toBeInTheDocument();
});

it("removes own cached amounts after a read failure instead of reporting an unlimited budget", async () => {
  auth.userRole = "Internal User";
  const { state } = serve();
  renderWithProviders(<WalletPanel />);
  const section = await screen.findByRole("region", { name: "Your model budgets" });
  await within(section).findByText("public/alpha");
  state.listStatus = 503;
  await userEvent.setup().click(within(section).getByRole("button", { name: "Refresh model budgets" }));
  expect(await within(section).findByText("Unable to load your model budgets")).toBeInTheDocument();
  expect(within(section).queryByText("public/alpha")).not.toBeInTheDocument();
  expect(within(section).queryByText("No matching model budgets")).not.toBeInTheDocument();
});

it("keeps expired windows read-only while preserving their displayed late charges", async () => {
  const { state } = serve();
  state.rows = [
    {
      ...budget,
      state: "expired",
      spent_usd: "31.000000000",
      held_usd: "2.000000000",
      remaining_usd: "0.000000000",
      overrun_usd: "3.000000000",
    },
  ];
  renderWithProviders(<AdminWalletsPanel />);
  await openAdmin();
  expect(screen.getByRole("button", { name: "Edit budget for public/alpha" })).toBeDisabled();
  expect(screen.getByText("Above limit: $3.00")).toBeInTheDocument();
});

it("does not mount a direct administrator budget dialog for a viewer", () => {
  auth.isViewOnly = true;
  const { calls } = serve();
  renderWithProviders(<AdminModelBudgetsDialog userId="foreign-account" onClose={vi.fn()} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(calls).toHaveLength(0);
});

it("selects an exact model from the server's second page and clears an earlier selection", async () => {
  const { calls, state } = serve();
  state.rows = [];
  state.modelTotal = 26;
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "New model budget" }));
  await screen.findByRole("option", { name: "public/alpha" });
  fireEvent.change(screen.getByLabelText("Public model"), { target: { value: "public/alpha" } });
  await user.click(screen.getByRole("button", { name: "Next page" }));
  await waitFor(() => expect(screen.queryByRole("option", { name: "public/alpha" })).not.toBeInTheDocument());
  expect(screen.getByLabelText("Public model")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Create budget" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Public model"), { target: { value: "public/beta" } });
  fireEvent.change(screen.getByLabelText("Budget limit (USD)"), { target: { value: "12" } });
  fillEnd();
  await user.click(screen.getByRole("button", { name: "Create budget" }));
  await screen.findByRole("button", { name: "Edit budget for public/beta" });
  const modelRequests = calls.filter((call) => call.url.pathname.endsWith("/model-budgets/models"));
  expect(modelRequests.at(-1)?.url.searchParams.get("offset")).toBe("25");
  expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({ model: "public/beta" });
});

it("does not enable creation when public model authority is unavailable", async () => {
  const { calls, state } = serve();
  state.modelsStatus = 503;
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "New model budget" }));
  expect(await screen.findByText("Unable to load public models")).toBeInTheDocument();
  expect(screen.queryByLabelText("Public model")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create budget" })).toBeDisabled();
  expect(calls.some((call) => call.method === "POST")).toBe(false);
});

it("preserves counters when enforcement is paused and resumed", async () => {
  const { calls } = serve();
  renderWithProviders(<AdminWalletsPanel />);
  const user = await openAdmin();
  await user.click(screen.getByRole("button", { name: "Edit budget for public/alpha" }));
  await user.click(screen.getByRole("checkbox", { name: "Enforce this budget" }));
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  await screen.findByText("Enforcement paused");
  expect(calls.find((call) => call.method === "PATCH")?.body).toMatchObject({ enabled: false, expected_version: 1 });
  expect(screen.getByText("$5.00")).toBeInTheDocument();
  expect(screen.getByText("$2.00")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Edit budget for public/alpha" }));
  expect(screen.getByRole("checkbox", { name: "Enforce this budget" })).not.toBeChecked();
  await user.click(screen.getByRole("checkbox", { name: "Enforce this budget" }));
  await user.click(screen.getByRole("button", { name: "Save budget" }));
  await screen.findByText("active");
  expect(calls.filter((call) => call.method === "PATCH").at(-1)?.body).toMatchObject({
    enabled: true,
    expected_version: 2,
  });
  expect(screen.getByText("$5.00")).toBeInTheDocument();
  expect(screen.getByText("$2.00")).toBeInTheDocument();
});

it("ends browsing at the server offset boundary and asks to narrow a larger search", async () => {
  auth.userRole = "Internal User";
  const { calls, state } = serve();
  state.budgetTotal = 10_026;
  renderWithProviders(<WalletPanel />, { searchParams: { budget_page: "401" } });
  const section = await screen.findByRole("region", { name: "Your model budgets" });
  await within(section).findByText("public/alpha");
  expect(within(section).getByRole("button", { name: "Next page" })).toBeDisabled();
  expect(within(section).getByText(/Narrow your search/)).toBeInTheDocument();
  const request = calls.find((call) => call.url.pathname === "/retail/model-budgets");
  expect(request?.url.searchParams.get("offset")).toBe("10000");
});
