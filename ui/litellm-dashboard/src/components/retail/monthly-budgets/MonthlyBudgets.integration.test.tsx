import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import AdminModelBudgetsDialog from "../model-budgets/AdminModelBudgetsDialog";
import CustomerModelBudgets from "../model-budgets/CustomerModelBudgets";
import type { AdminModelBudget } from "../model-budgets/types";
import AdminMonthlyBudgets from "./AdminMonthlyBudgets";
import type { AdminMonthlyBudget, CreateMonthlyBudget, UpdateMonthlyBudget } from "./types";

const auth = vi.hoisted(() => ({ userRole: "Admin", isViewOnly: false, userId: "actor-one" }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ ...auth, accessToken: "fixture-session", isAuthorized: true, isLoading: false }),
}));

const plan: AdminMonthlyBudget = {
  id: "7dd14fb4-77c4-428f-b944-59a7e7f976bd",
  user_id: "account-one",
  model: "public-model",
  currency: "USD",
  timezone: "UTC",
  starts_at: "2020-01-01T00:00:00Z",
  stops_at: null,
  limit_usd: "0.000000009",
  limit_effective_from: "2099-01-01T00:00:00Z",
  version: 3,
  state: "active",
  current_window_ready: true,
  created_at: "2020-01-01T00:00:00Z",
  created_by: "private-admin-marker",
  updated_at: "2020-01-01T00:00:00Z",
  updated_by: "private-admin-marker",
};
const child: AdminModelBudget = {
  id: "587fca26-eea1-4573-9c77-2fc2ccbb52fa",
  user_id: plan.user_id,
  model: plan.model,
  effective_from: "2026-11-01T00:00:00Z",
  ends_at: "2026-12-01T00:00:00Z",
  limit_usd: "1.000000000",
  spent_usd: "0.000000000",
  held_usd: "0.000000000",
  remaining_usd: "1.000000000",
  overrun_usd: "0.000000000",
  enabled: true,
  config_version: 1,
  accounting_version: 0,
  accounted_at: null,
  predecessor_id: null,
  successor_id: null,
  recurrence_id: plan.id,
  state: "active",
  source: "management_api",
  created_at: plan.created_at,
  created_by: "private-admin-marker",
  updated_at: plan.updated_at,
  updated_by: "private-admin-marker",
};
type Call = { url: URL; method: string; body: unknown; headers: Headers };
type State = {
  enabled: boolean;
  rows: AdminMonthlyBudget[];
  fixed: AdminModelBudget[];
  listStatus: number;
  writeStatus: number;
  total: number;
};
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}
function page<T>(items: T[], total = items.length) {
  return { items, total, limit: 25, offset: 0 };
}
function serve(overrides: Partial<State> = {}) {
  const state: State = {
    enabled: true,
    rows: [plan],
    fixed: [],
    listStatus: 200,
    writeStatus: 200,
    total: 1,
    ...overrides,
  };
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const call: Call = {
        url: new URL(input),
        method: options?.method ?? "GET",
        body: options?.body ? (JSON.parse(String(options.body)) as unknown) : null,
        headers: new Headers(options?.headers),
      };
      calls.push(call);
      const path = call.url.pathname;
      if (path === "/retail/config")
        return response({ model_budgets_enabled: true, monthly_budgets_enabled: state.enabled });
      if (path.endsWith("/model-budgets/models")) return response(page([{ model: "public-model" }]));
      if (path.endsWith("/model-budgets")) return response(page(state.fixed));
      if (call.method !== "GET") {
        if (state.writeStatus !== 200)
          return response({ detail: "private provider diagnostic must stay hidden" }, state.writeStatus);
        if (path.endsWith("/stop"))
          state.rows = state.rows.map((row) => ({
            ...row,
            state: "stopping",
            stops_at: "2099-01-01T00:00:00Z",
            version: row.version + 1,
          }));
        else if (call.method === "PATCH") {
          const payload = call.body as UpdateMonthlyBudget;
          state.rows = state.rows.map((row) => ({ ...row, limit_usd: payload.limit_usd, version: row.version + 1 }));
        } else {
          const payload = call.body as CreateMonthlyBudget;
          state.rows = [
            {
              ...plan,
              model: payload.model,
              timezone: payload.timezone,
              limit_usd: payload.limit_usd,
              state: "scheduled",
              version: 1,
            },
          ];
        }
        return response(state.rows[0], call.method === "POST" && !path.endsWith("/stop") ? 201 : 200);
      }
      if (path.includes("monthly-budgets"))
        return state.listStatus === 200
          ? response(page(state.rows, state.total))
          : response({ detail: "Access revoked" }, state.listStatus);
      throw new Error(`Unexpected monthly request ${path}`);
    }),
  );
  return { state, calls };
}
beforeEach(() => {
  auth.userRole = "Admin";
  auth.isViewOnly = false;
  auth.userId = "actor-one";
  testQueryClient.clear();
});
afterEach(() => {
  testQueryClient.clear();
  vi.unstubAllGlobals();
});
const noSaving = () => undefined;
async function monthlyTab() {
  renderWithProviders(<AdminModelBudgetsDialog userId="account-one" onClose={() => undefined} />);
  await userEvent.click(await screen.findByRole("tab", { name: "Monthly plans" }));
  await screen.findByRole("button", { name: "New monthly plan" });
}

it("adds a discoverable monthly entry only when enabled and avoids monthly calls in fixed-window mode", async () => {
  const { state, calls } = serve();
  const rendered = renderWithProviders(<AdminModelBudgetsDialog userId="account-one" onClose={() => undefined} />);
  await screen.findByRole("button", { name: "New model budget" });
  expect(screen.getByRole("tab", { name: "Monthly plans" })).toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("monthly-budgets"))).toBe(false);
  rendered.unmount();
  testQueryClient.clear();
  state.enabled = false;
  renderWithProviders(<AdminModelBudgetsDialog userId="account-one" onClose={() => undefined} />, {
    searchParams: { budget_mode: "monthly" },
  });
  await screen.findByRole("button", { name: "New model budget" });
  expect(screen.queryByRole("tab", { name: "Monthly plans" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("monthly-budgets"))).toBe(false);
});

it("protects standalone monthly management from viewer hosts and cached admin data", async () => {
  auth.isViewOnly = true;
  const { calls } = serve();
  testQueryClient.setQueryData(
    ["retail", "monthly-budgets", "actor-one", "admin", "account-one", { monthly_q: "", monthly_page: 1 }],
    page([plan]),
  );
  renderWithProviders(<AdminMonthlyBudgets userId="account-one" onSavingChange={noSaving} />);
  expect(screen.queryByText("public-model")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New monthly plan" })).not.toBeInTheDocument();
  expect(calls).toHaveLength(0);
});

it("creates exact monthly terms with nine-decimal USD and reuses an uncertain operation key", async () => {
  const { state, calls } = serve({ rows: [], writeStatus: 503, total: 0 });
  await monthlyTab();
  await userEvent.click(screen.getByRole("button", { name: "New monthly plan" }));
  fireEvent.change(await screen.findByLabelText("Public model"), { target: { value: "public-model" } });
  fireEvent.change(screen.getByLabelText("Monthly limit (USD)"), { target: { value: "0.000000009" } });
  const month = `${new Date().getUTCFullYear() + 2}-01`;
  fireEvent.change(screen.getByLabelText("First full month"), { target: { value: month } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Create monthly plan" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "Create monthly plan" }));
  await screen.findByText("Unable to save monthly plan");
  expect(screen.queryByText("private provider diagnostic must stay hidden")).not.toBeInTheDocument();
  state.writeStatus = 200;
  await userEvent.click(screen.getByRole("button", { name: "Create monthly plan" }));
  await screen.findByRole("button", { name: "New monthly plan" });
  const writes = calls.filter((call) => call.method === "POST");
  expect(writes).toHaveLength(2);
  const payload = {
    model: "public-model",
    start_month: month,
    timezone: "Asia/Shanghai",
    limit_usd: "0.000000009",
    currency: "USD",
  };
  expect(writes.map((call) => call.body)).toEqual([payload, payload]);
  expect(writes[0].headers.get("Idempotency-Key")).toBeTruthy();
  expect(writes[1].headers.get("Idempotency-Key")).toBe(writes[0].headers.get("Idempotency-Key"));
});

it("keeps the current month frozen and requires a reload after a version conflict", async () => {
  const { state, calls } = serve({ writeStatus: 409 });
  await monthlyTab();
  await userEvent.click(screen.getByRole("button", { name: "Change monthly limit for public-model" }));
  expect(screen.getByText(/This month’s window is unchanged/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Monthly limit (USD)"), { target: { value: "2.000000001" } });
  await userEvent.click(screen.getByRole("button", { name: "Save future limit" }));
  await screen.findByText("Monthly plan needs review");
  expect(screen.getByRole("button", { name: "Save future limit" })).toBeDisabled();
  state.rows = [{ ...plan, version: 4 }];
  state.writeStatus = 200;
  await userEvent.click(screen.getByRole("button", { name: "Reload monthly plans" }));
  await userEvent.click(await screen.findByRole("button", { name: "Change monthly limit for public-model" }));
  fireEvent.change(screen.getByLabelText("Monthly limit (USD)"), { target: { value: "2.000000001" } });
  await userEvent.click(screen.getByRole("button", { name: "Save future limit" }));
  await screen.findByRole("button", { name: "New monthly plan" });
  expect(calls.filter((call) => call.method === "PATCH").map((call) => call.body)).toEqual([
    { limit_usd: "2.000000001", expected_version: 3 },
    { limit_usd: "2.000000001", expected_version: 4 },
  ]);
});

it("stops at month end through an explicit confirmation and hides further changes", async () => {
  const { calls } = serve();
  await monthlyTab();
  await userEvent.click(screen.getByRole("button", { name: "Stop monthly plan for public-model" }));
  expect(screen.getByText(/This month stays frozen/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Monthly limit (USD)")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Confirm stop" }));
  await screen.findByText("Stops at month end");
  expect(screen.queryByRole("button", { name: "Change monthly limit for public-model" })).not.toBeInTheDocument();
  const write = calls.find((call) => call.url.pathname.endsWith("/stop"));
  expect(write?.body).toEqual({ expected_version: 3 });
  expect(write?.method).toBe("POST");
});

it("removes cached rows and an editor when a fresh authority check fails", async () => {
  const { state } = serve();
  await monthlyTab();
  await userEvent.click(screen.getByRole("button", { name: "Change monthly limit for public-model" }));
  state.listStatus = 403;
  await testQueryClient.invalidateQueries({ queryKey: ["retail", "monthly-budgets"] });
  await screen.findByText("Unable to load monthly plans");
  expect(screen.queryByLabelText("Monthly limit (USD)")).not.toBeInTheDocument();
  expect(screen.queryByText("public-model")).not.toBeInTheDocument();
});

it("projects personal monthly plans read-only and distinguishes a missing window from unlimited usage", async () => {
  auth.userRole = "Internal User";
  const { calls } = serve({ rows: [{ ...plan, current_window_ready: false }] });
  renderWithProviders(<CustomerModelBudgets />);
  const section = await screen.findByRole("region", { name: "Your monthly model budgets" });
  expect(await within(section).findByText("$0.000000009")).toBeInTheDocument();
  expect(within(section).getByText("Current window unavailable")).toBeInTheDocument();
  expect(within(section).queryByRole("button", { name: /Change|Stop|Cancel|New monthly/ })).not.toBeInTheDocument();
  expect(screen.queryByText("private-admin-marker")).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
  expect(
    calls
      .filter((call) => call.url.pathname.endsWith("monthly-budgets"))
      .map((call) => call.url.searchParams.get("user_id")),
  ).toEqual([null]);
});

it.each(["public-model", "p".repeat(150)])(
  "redirects recurring child-window actions to bounded monthly search for %s",
  async (model) => {
    serve({ fixed: [{ ...child, model }], rows: [{ ...plan, model }] });
    renderWithProviders(<AdminModelBudgetsDialog userId="account-one" onClose={() => undefined} />);
    await screen.findByText("Calendar-month plan");
    expect(screen.queryByRole("button", { name: `Edit budget for ${model}` })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `Schedule next window for ${model}` })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: `Manage monthly plan for ${model}` }));
    expect(await screen.findByRole("button", { name: "New monthly plan" })).toBeInTheDocument();
    expect(screen.getByLabelText("Search monthly plan models")).toHaveValue(model.slice(0, 100));
  },
);

it("labels disabled monthly child windows as cancelled while retaining manual paused status and amounts", async () => {
  serve({
    fixed: [
      { ...child, enabled: false, state: "scheduled", spent_usd: "0.125000000" },
      { ...child, id: "manual-window", model: "manual-model", recurrence_id: null, enabled: false, state: "scheduled" },
    ],
  });
  renderWithProviders(<AdminModelBudgetsDialog userId="account-one" onClose={() => undefined} />);
  const monthly = within(await screen.findByRole("row", { name: /public-model/ }));
  expect(monthly.getByText("Cancelled by monthly plan")).toBeInTheDocument();
  expect(monthly.queryByText("Scheduled")).not.toBeInTheDocument();
  expect(monthly.queryByText("Enforcement paused")).not.toBeInTheDocument();
  expect(monthly.getByText("$0.125")).toBeInTheDocument();
  expect(monthly.queryByRole("button", { name: "Edit budget for public-model" })).not.toBeInTheDocument();
  expect(monthly.queryByRole("button", { name: "Schedule next window for public-model" })).not.toBeInTheDocument();
  const manual = within(screen.getByRole("row", { name: /manual-model/ }));
  expect(manual.getByText("Scheduled")).toBeInTheDocument();
  expect(manual.getByText("Enforcement paused")).toBeInTheDocument();
  expect(manual.getByRole("button", { name: "Edit budget for manual-model" })).toBeEnabled();
});

it("treats rejected terms as validation rather than an uncertain successful write", async () => {
  serve({ writeStatus: 422 });
  await monthlyTab();
  await userEvent.click(screen.getByRole("button", { name: "Change monthly limit for public-model" }));
  fireEvent.change(screen.getByLabelText("Monthly limit (USD)"), { target: { value: "1" } });
  await userEvent.click(screen.getByRole("button", { name: "Save future limit" }));
  expect(await screen.findByText(/The server did not accept these terms/)).toBeInTheDocument();
  expect(screen.queryByText(/The result could not be confirmed/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save future limit" })).toBeEnabled();
});

it("persists bounded monthly model search and pagination in URL state", async () => {
  const { calls } = serve({ total: 26 });
  renderWithProviders(<AdminModelBudgetsDialog userId="account-one" onClose={() => undefined} />, {
    searchParams: { budget_mode: "monthly", monthly_q: "public", monthly_page: "2" },
  });
  await screen.findByRole("button", { name: "New monthly plan" });
  const first = calls.find((call) => call.url.pathname.endsWith("monthly-budgets"));
  expect(first?.url.searchParams.get("q")).toBe("public");
  expect(first?.url.searchParams.get("offset")).toBe("25");
  fireEvent.change(screen.getByLabelText("Search monthly plan models"), { target: { value: "other" } });
  await waitFor(() =>
    expect(
      calls.some((call) => call.url.searchParams.get("q") === "other" && call.url.searchParams.get("offset") === "0"),
    ).toBe(true),
  );
});
