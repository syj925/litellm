import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import RetailOperationAuditPanel from "../audit/RetailOperationAuditPanel";
import RetailAuditDeliveryPanel from "./RetailAuditDeliveryPanel";
import type { AuditDeliveryJob, AuditDeliveryStatus } from "./types";

const auth = vi.hoisted(() => ({ userRole: "Admin", isViewOnly: false }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    ...auth,
    accessToken: "test-session",
    userId: "fixture-admin",
    isAuthorized: true,
    isLoading: false,
  }),
}));

const initialStatus: AuditDeliveryStatus = {
  enabled: true,
  config_version: 3,
  configuration_matches: true,
  destination_configured: true,
  configuration_error: null,
  activated_at: "2026-11-04T00:00:00Z",
  retained_count: 1,
  overflow_count: 0,
  last_overflow_at: null,
  counts: { pending: 0, processing: 0, delivered: 0, failed: 1, discarded: 0 },
  last_delivered_at: null,
  last_failure_at: "2026-11-04T00:01:00Z",
  queue_capacity: 10000,
  max_attempts: 5,
  retention_days: 7,
  global_concurrency: 2,
};
const initialJob: AuditDeliveryJob = {
  id: "ef51b72b-4343-4c83-a49e-6263bb29b2b5",
  event_id: "4eb69e2c-cdc5-4306-897a-60d38d701c5f",
  action: "model_budget.created",
  resource_type: "model_budget",
  occurred_at: "2026-11-04T00:00:00Z",
  state: "failed",
  attempts: 5,
  version: 7,
  created_at: "2026-11-04T00:00:00Z",
  completed_at: "2026-11-04T00:01:00Z",
  error_code: "timeout",
  next_attempt_at: null,
  configuration_matches: true,
};
type Call = { path: string; url: URL; method: string; body: unknown; headers: Headers };
type Server = {
  feature: boolean;
  status: AuditDeliveryStatus;
  rows: AuditDeliveryJob[];
  writeStatus: number;
  readStatus: number;
  total: number;
  networkFailures: number;
};
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function serveMutation(call: Call, state: Server) {
  if (state.networkFailures > 0) {
    state.networkFailures -= 1;
    throw new TypeError("Failed to fetch");
  }
  if (state.writeStatus !== 200) return json({ detail: "private-receiver-marker" }, state.writeStatus);
  if (call.method === "PATCH") {
    const payload = call.body as { enabled: boolean; expected_version: number };
    state.status = { ...state.status, enabled: payload.enabled, config_version: payload.expected_version + 1 };
    return json(state.status);
  }
  if (call.path.endsWith("/discard")) {
    state.rows = state.rows.map((row) => ({ ...row, state: "discarded" }));
    state.status = {
      ...state.status,
      config_version: state.status.config_version + 1,
      counts: { ...state.status.counts, failed: 0, discarded: 1 },
    };
    return json(state.status);
  }
  state.rows = state.rows.map((row) => ({
    ...row,
    state: "pending",
    attempts: 0,
    version: row.version + 1,
    error_code: null,
  }));
  state.status = { ...state.status, counts: { ...state.status.counts, failed: 0, pending: 1 } };
  return json(state.rows[0]);
}
function serve() {
  const calls: Call[] = [];
  const state: Server = {
    feature: true,
    status: structuredClone(initialStatus),
    rows: [initialJob],
    writeStatus: 200,
    readStatus: 200,
    total: 1,
    networkFailures: 0,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const call = {
        url: new URL(input),
        path: new URL(input).pathname,
        method: options?.method ?? "GET",
        body: options?.body ? (JSON.parse(String(options.body)) as unknown) : null,
        headers: new Headers(options?.headers),
      };
      calls.push(call);
      if (call.path === "/retail/config")
        return json({ audit_delivery_enabled: state.feature, operation_audit_enabled: true });
      if (["/retail/admin/operation-events", "/retail/organizations"].includes(call.path)) {
        const page = { items: [], total: 0, limit: 25, offset: 0 };
        return json(page);
      }
      if (call.method !== "GET") return serveMutation(call, state);
      if (state.readStatus !== 200) return json({ detail: "private-receiver-marker" }, state.readStatus);
      if (call.path.endsWith("/deliveries")) {
        const page = {
          items: state.rows,
          total: state.total,
          limit: 25,
          offset: Number(call.url.searchParams.get("offset")),
        };
        return json(page);
      }
      return json(state.status);
    }),
  );
  return { calls, state };
}
function mutations(calls: Call[]) {
  return calls.filter((call) => call.method !== "GET");
}

beforeEach(() => {
  auth.userRole = "Admin";
  auth.isViewOnly = false;
  testQueryClient.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  testQueryClient.clear();
});

it("shows safe retained results and requires a server-configured destination", async () => {
  const { state } = serve();
  state.status = {
    ...state.status,
    enabled: false,
    destination_configured: false,
    configuration_error: "not_configured",
  };
  renderWithProviders(<RetailAuditDeliveryPanel />);
  expect(await screen.findByText("Operator configuration required")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enable delivery", exact: true })).toBeDisabled();
  expect(screen.getByText("The receiver did not respond in time.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

it.each([
  { userRole: "Internal User", isViewOnly: false },
  { userRole: "proxy_admin_viewer", isViewOnly: false },
  { userRole: "Admin", isViewOnly: true },
])("does not load delivery data for %o", async (role) => {
  const { calls } = serve();
  Object.assign(auth, role);
  renderWithProviders(<RetailAuditDeliveryPanel />);
  expect(screen.getByText("Gateway administrator access required")).toBeInTheDocument();
  expect(calls).toHaveLength(0);
});

it("does not load delivery data when the feature is disabled", async () => {
  const { calls, state } = serve();
  state.feature = false;
  renderWithProviders(<RetailAuditDeliveryPanel />);
  expect(await screen.findByText("Audit delivery is not enabled")).toBeInTheDocument();
  expect(calls.map((call) => call.path)).toEqual(["/retail/config"]);
});

it("sends the current settings version when pausing", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await userEvent.click(await screen.findByRole("button", { name: "Pause delivery", exact: true }));
  expect(await screen.findByRole("button", { name: "Enable delivery", exact: true })).toBeEnabled();
  expect(mutations(calls)[0].body).toEqual({ enabled: false, expected_version: initialStatus.config_version });
  expect(mutations(calls)[0].headers.get("Idempotency-Key")).toBeTruthy();
});

it("retries only the selected matching failed delivery with its record version", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await userEvent.click(await screen.findByRole("button", { name: `Retry delivery ${initialJob.id}`, exact: true }));
  expect(await within(await screen.findByRole("table")).findByText("Queued")).toBeInTheDocument();
  const call = mutations(calls)[0];
  expect(call.path).toBe(`/retail/admin/audit-delivery/deliveries/${initialJob.id}/retry`);
  expect(call.body).toEqual({ expected_version: initialJob.version });
});

it("requires a confirmation dialog before discarding and uses the settings version", async () => {
  const { calls, state } = serve();
  state.status = { ...state.status, enabled: false };
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await userEvent.click(await screen.findByRole("button", { name: "Discard outstanding", exact: true }));
  const dialog = screen.getByRole("dialog", { name: "Discard outstanding deliveries?", exact: true });
  expect(mutations(calls)).toHaveLength(0);
  await userEvent.click(within(dialog).getByRole("button", { name: "Discard outstanding deliveries", exact: true }));
  await waitFor(() => expect(mutations(calls)).toHaveLength(1));
  expect(mutations(calls)[0].body).toEqual({
    expected_version: initialStatus.config_version,
    confirmation: "discard_pending",
  });
});

it("blocks rotation and retry for outstanding work from an old configuration", async () => {
  const { state } = serve();
  state.status = { ...state.status, enabled: false, configuration_matches: false };
  state.rows = [{ ...initialJob, configuration_matches: false }];
  renderWithProviders(<RetailAuditDeliveryPanel />);
  expect(await screen.findByText("Receiver configuration changed")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enable delivery", exact: true })).toBeDisabled();
  expect(screen.getByRole("button", { name: `Retry delivery ${initialJob.id}`, exact: true })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Discard outstanding", exact: true })).toBeEnabled();
});

it("records overflow visibly without claiming local audit was lost", async () => {
  const { state } = serve();
  state.status = { ...state.status, overflow_count: 4 };
  renderWithProviders(<RetailAuditDeliveryPanel />);
  expect(await screen.findByText("External delivery gaps recorded")).toBeInTheDocument();
  expect(screen.getByText(/4 events did not enter the full external queue/)).toBeInTheDocument();
  expect(screen.getByText(/Their local audit records remain saved/)).toBeInTheDocument();
});

it("keeps pagination and state filters in the URL", async () => {
  const { calls, state } = serve();
  state.total = 40;
  const onUrlUpdate = vi.fn();
  const providers = { searchParams: { delivery_state: "failed", delivery_page: "2" }, onUrlUpdate };
  renderWithProviders(<RetailAuditDeliveryPanel />, providers);
  await screen.findByRole("combobox", { name: "Delivery status", exact: true });
  const listed = calls.find((call) => call.path.endsWith("/deliveries"));
  expect(listed?.url.searchParams.get("offset")).toBe("25");
  expect(listed?.url.searchParams.get("state")).toBe("failed");
  fireEvent.change(screen.getByRole("combobox", { name: "Delivery status", exact: true }), {
    target: { value: "pending" },
  });
  await waitFor(() => expect(onUrlUpdate).toHaveBeenCalled());
  await waitFor(() =>
    expect(
      calls.some(
        (call) => call.url.searchParams.get("state") === "pending" && call.url.searchParams.get("offset") === "0",
      ),
    ).toBe(true),
  );
});

it("keeps an uncertain retry on the same idempotency key", async () => {
  const { calls, state } = serve();
  state.networkFailures = 1;
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await userEvent.click(await screen.findByRole("button", { name: `Retry delivery ${initialJob.id}`, exact: true }));
  await screen.findByText("Unable to update delivery");
  await userEvent.click(screen.getByRole("button", { name: `Retry delivery ${initialJob.id}`, exact: true }));
  await waitFor(() => expect(mutations(calls)).toHaveLength(2));
  expect(mutations(calls)[0].headers.get("Idempotency-Key")).toBe(mutations(calls)[1].headers.get("Idempotency-Key"));
});

it("requires reload after a version conflict and hides raw diagnostics", async () => {
  const { calls, state } = serve();
  state.writeStatus = 409;
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await userEvent.click(await screen.findByRole("button", { name: "Pause delivery", exact: true }));
  expect(await screen.findByText("Delivery needs review")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Pause delivery", exact: true })).toBeDisabled();
  expect(screen.queryByText("private-receiver-marker")).not.toBeInTheDocument();
  expect(mutations(calls)).toHaveLength(1);
  state.writeStatus = 200;
  await userEvent.click(screen.getByRole("button", { name: "Reload deliveries", exact: true }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Pause delivery", exact: true })).toBeEnabled());
});

it("removes cached delivery content after mutation authority is revoked", async () => {
  const { state } = serve();
  state.writeStatus = 403;
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await userEvent.click(await screen.findByRole("button", { name: "Pause delivery", exact: true }));
  expect(await screen.findByText("Audit delivery is unavailable")).toBeInTheDocument();
  expect(screen.queryByText("Model budget created")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: `Retry delivery ${initialJob.id}`, exact: true }),
  ).not.toBeInTheDocument();
});

it("does not offer a retry for a delivery whose lifetime expired", async () => {
  const { state } = serve();
  state.rows = [{ ...initialJob, error_code: "expired" }];
  renderWithProviders(<RetailAuditDeliveryPanel />);
  expect(await screen.findByText("The delivery exceeded its retention window.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: `Retry delivery ${initialJob.id}`, exact: true })).toBeDisabled();
});

it("hides retained content when a reload can no longer verify access", async () => {
  const { state } = serve();
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await screen.findByText("Model budget created");
  state.readStatus = 403;
  await userEvent.click(screen.getByRole("button", { name: "Reload deliveries", exact: true }));
  expect(await screen.findByText("Audit delivery is unavailable")).toBeInTheDocument();
  expect(screen.queryByText("Model budget created")).not.toBeInTheDocument();
  expect(screen.queryByText("private-receiver-marker")).not.toBeInTheDocument();
});

it("keeps detailed delivery limits collapsed until requested", async () => {
  serve();
  renderWithProviders(<RetailAuditDeliveryPanel />);
  await screen.findByRole("button", { name: "Pause delivery", exact: true });
  const rules = screen.getByText(/The queue and receipts hold at most/);
  expect(rules).not.toBeVisible();
  await userEvent.click(screen.getByText("Delivery scope and limits", { exact: true }));
  expect(rules).toBeVisible();
});

it("links the writable administrator from operation audit to the delivery page", async () => {
  serve();
  renderWithProviders(<RetailOperationAuditPanel />);
  expect(await screen.findByRole("link", { name: "Audit delivery", exact: true })).toHaveAttribute(
    "href",
    "/ui/audit-delivery",
  );
});

it.each(["Internal User", "proxy_admin_viewer"])(
  "does not expose the operation-audit delivery entry to %s",
  async (userRole) => {
    serve();
    auth.userRole = userRole;
    renderWithProviders(<RetailOperationAuditPanel />);
    await screen.findByRole("heading", { name: "Operation Audit", exact: true });
    expect(screen.queryByRole("link", { name: "Audit delivery", exact: true })).not.toBeInTheDocument();
  },
);
