import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import AdminWalletsPanel from "../AdminWalletsPanel";
import RetailOrganizationsPanel from "../organizations/RetailOrganizationsPanel";
import BillingOrganizationDialog from "./BillingOrganizationDialog";
import RetailOrganizationConsumptionPanel from "./RetailOrganizationConsumptionPanel";
import type { BillingOrganization, OrganizationConsumption } from "./types";

const auth = vi.hoisted(() => ({ userRole: "Admin", isViewOnly: false, isAuthorized: true }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ ...auth, accessToken: "test-session", userId: "fixture-admin", isLoading: false }),
}));

const org = {
  id: "org-one",
  name: "Example organization",
  enabled: true,
  version: 1,
  member_role: null,
  can_edit: true,
  can_associate_members: true,
  can_manage_members: true,
  can_view_events: true,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
};
const assignment: BillingOrganization = {
  user_id: "customer-one",
  organization_id: "org-old",
  organization_name: "Previous organization",
  organization_enabled: false,
  membership_enabled: true,
  version: 4,
  effective: false,
  state: "organization_disabled",
  updated_at: "2026-11-01T00:00:00Z",
  updated_by: "private-operator",
};
const report: OrganizationConsumption = {
  organization_id: org.id,
  organization_name: org.name,
  organization_enabled: false,
  axis: "admission",
  snapshot_at: "2026-11-04T01:00:00Z",
  start: "2026-11-01T00:00:00Z",
  end: "2026-11-04T00:00:00Z",
  partial: true,
  live: true,
  max_requests: 10000,
  total: 30,
  limit: 25,
  offset: 0,
  summary: {
    accepted_requests: 30,
    settled_requests: 26,
    released_requests: 1,
    pending_requests: 1,
    unknown_requests: 1,
    conflict_requests: 1,
    settled_usd: "9007199254740993.000000001",
    settled_nanos: "9007199254740993000000001",
  },
  items: ["settled", "released", "pending", "unknown", "conflict"].map((state, index) => ({
    request_id: `request-${index}`,
    user_id: "customer-one",
    model: "public/alpha",
    captured_at: "2026-11-02T00:00:00Z",
    state: state as OrganizationConsumption["items"][number]["state"],
    settled_usd: state === "settled" ? "0.000000001" : null,
    assignment_version: 4,
    organization_version: 1,
    membership_version: 2,
  })),
};
type Call = { url: URL; method: string; body: unknown; headers: Headers };
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function page<T>(items: T[], total = items.length, offset = 0) {
  return { items, total, limit: 25, offset };
}
function patchAssignment(
  call: Call,
  state: { networkFailures: number; writeStatus: number; assignment: BillingOrganization },
) {
  if (state.networkFailures > 0) {
    state.networkFailures -= 1;
    throw new TypeError("Failed to fetch");
  }
  if (state.writeStatus !== 200) return json({ detail: "private-source-marker" }, state.writeStatus);
  const body = call.body as { organization_id: string | null; expected_version: number };
  state.assignment = {
    ...assignment,
    organization_id: body.organization_id,
    organization_name: body.organization_id ? org.name : null,
    organization_enabled: true,
    state: body.organization_id ? "active" : "unassigned",
    version: body.expected_version + 1,
  };
  return json(state.assignment);
}
function serve() {
  const calls: Call[] = [];
  const state = {
    enabled: true,
    assignment: structuredClone(assignment),
    report: structuredClone(report),
    readStatus: 200,
    pickerStatus: 200,
    writeStatus: 200,
    networkFailures: 0,
    pickerTotal: 30,
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
      if (path === "/retail/config")
        return json({ org_finance_enabled: state.enabled, organization_management_enabled: true });
      if (path === "/retail/admin/wallets")
        return json(
          page([
            {
              user_id: "customer-one",
              currency: "USD",
              balance: "100",
              available: "90",
              reserved: "10",
              total_credited: "120",
              total_spent: "20",
              pending_requests: 1,
              pricing_plan: { id: "standard", name: "Standard", multiplier: "1", discount: "1" },
            },
          ]),
        );
      if (path === "/retail/plans") return json({ items: [] });
      if (call.method === "PATCH") {
        return patchAssignment(call, state);
      }
      if (
        ["/retail/admin/organizations", "/retail/organizations"].includes(path) ||
        path.endsWith("/billing-organization/organizations")
      ) {
        if (state.pickerStatus !== 200) return json({ detail: "private-source-marker" }, state.pickerStatus);
        const offset = Number(call.url.searchParams.get("offset"));
        return json(
          page(offset ? [{ ...org, id: "org-two", name: "Second organization" }] : [org], state.pickerTotal, offset),
        );
      }
      if (state.readStatus !== 200) return json({ detail: "private-source-marker" }, state.readStatus);
      if (path.endsWith("/consumption")) return json(state.report);
      if (path.endsWith("/billing-organization")) return json(state.assignment);
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  return { state, calls };
}
function writes(calls: Call[]) {
  return calls.filter((call) => call.method === "PATCH");
}
beforeEach(() => {
  Object.assign(auth, { userRole: "Admin", isViewOnly: false, isAuthorized: true });
  testQueryClient.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  testQueryClient.clear();
});

it.each([
  { userRole: "Internal User", isViewOnly: false, isAuthorized: true },
  { userRole: "Org Admin", isViewOnly: false, isAuthorized: true },
  { userRole: "proxy_admin_viewer", isViewOnly: false, isAuthorized: true },
  { userRole: "Admin", isViewOnly: true, isAuthorized: true },
  { userRole: "Admin", isViewOnly: false, isAuthorized: false },
])("does not fetch organization finance for an unauthorized role %o", (role) => {
  const { calls } = serve();
  Object.assign(auth, role);
  renderWithProviders(
    <>
      <BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />
      <RetailOrganizationConsumptionPanel />
    </>,
  );
  expect(calls).toHaveLength(0);
});

it("does not load assignments or consumption when the feature is disabled", async () => {
  const { state, calls } = serve();
  state.enabled = false;
  renderWithProviders(
    <>
      <BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />
      <RetailOrganizationConsumptionPanel />
    </>,
  );
  expect(await screen.findAllByText("Organization finance is not enabled")).toHaveLength(2);
  expect(calls.every((call) => call.url.pathname === "/retail/config")).toBe(true);
});

it("requires an explicit choice even with an eligible membership and preserves an inactive current assignment", async () => {
  const { calls } = serve();
  renderWithProviders(<BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />);
  expect(await screen.findByText("Previous organization")).toBeInTheDocument();
  expect(screen.getByText("Inactive — organization disabled")).toBeInTheDocument();
  expect(await screen.findByRole("combobox", { name: "New billing organization" })).toHaveValue("");
  expect(screen.getByRole("button", { name: "Save assignment" })).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "organization:org-one" } });
  await userEvent.click(screen.getByRole("button", { name: "Save assignment" }));
  expect(await screen.findByText("Billing organization saved for future requests.")).toBeInTheDocument();
  expect(writes(calls)[0].body).toEqual({ organization_id: "org-one", expected_version: 4 });
  expect(writes(calls)[0].headers.get("Idempotency-Key")).toBeTruthy();
  expect(screen.queryByText("private-operator")).not.toBeInTheDocument();
});

it("clears assignment explicitly and reuses the idempotency key after an uncertain result", async () => {
  const { state, calls } = serve();
  state.networkFailures = 1;
  renderWithProviders(<BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />);
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "clear" } });
  await userEvent.click(screen.getByRole("button", { name: "Save assignment" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be confirmed");
  await userEvent.click(screen.getByRole("button", { name: "Save assignment" }));
  await screen.findByText("Billing organization saved for future requests.");
  expect(writes(calls).map((call) => call.body)).toEqual([
    { organization_id: null, expected_version: 4 },
    { organization_id: null, expected_version: 4 },
  ]);
  expect(writes(calls)[0].headers.get("Idempotency-Key")).toBe(writes(calls)[1].headers.get("Idempotency-Key"));
});

it("explains assignment eligibility validation without a consumption-window error", async () => {
  const { state } = serve();
  state.writeStatus = 422;
  renderWithProviders(<BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />);
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "organization:org-one" } });
  await userEvent.click(screen.getByRole("button", { name: "Save assignment" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Reload the assignment and choose an active membership");
  expect(screen.getByRole("alert")).not.toHaveTextContent("window");
  expect(screen.getByRole("alert")).not.toHaveTextContent("10,000");
  expect(screen.getByRole("alert")).not.toHaveTextContent("private-source-marker");
  await userEvent.click(screen.getByRole("button", { name: "Reload assignment" }));
  await waitFor(() => expect(screen.getByRole("combobox")).toBeEnabled());
  expect(screen.getByRole("combobox")).toHaveValue("");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("requires a reload after a version conflict", async () => {
  const { state } = serve();
  state.writeStatus = 409;
  renderWithProviders(<BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />);
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "clear" } });
  await userEvent.click(screen.getByRole("button", { name: "Save assignment" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Reload before saving again");
  expect(screen.getByRole("button", { name: "Save assignment" })).toBeDisabled();
  state.assignment.version = 9;
  state.writeStatus = 200;
  await userEvent.click(screen.getByRole("button", { name: "Reload assignment" }));
  await waitFor(() => expect(screen.getByRole("combobox")).toBeEnabled());
  expect(screen.getByRole("combobox")).toHaveValue("");
});

it("discards a staged choice when the eligible directory is paged", async () => {
  const { calls } = serve();
  renderWithProviders(<BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />);
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "organization:org-one" } });
  await userEvent.click(screen.getByRole("button", { name: "Next page" }));
  await screen.findByRole("option", { name: "Second organization" });
  expect(screen.getByRole("combobox")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Save assignment" })).toBeDisabled();
  expect(calls.some((call) => call.url.searchParams.get("offset") === "25")).toBe(true);
});

it("hides assignment cache after a write authority failure", async () => {
  const { state } = serve();
  state.writeStatus = 403;
  renderWithProviders(<BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />);
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "clear" } });
  await userEvent.click(screen.getByRole("button", { name: "Save assignment" }));
  await screen.findByText("Assignment access changed. Reload to check your current access.");
  expect(screen.queryByText("Previous organization")).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByText("private-source-marker")).not.toBeInTheDocument();
});

it("reloads eligibility after access is restored without revealing the old assignment cache", async () => {
  const { state } = serve();
  state.pickerStatus = 403;
  renderWithProviders(<BillingOrganizationDialog userId="customer-one" onClose={vi.fn()} />);
  await screen.findByText("Assignment access changed. Reload to check your current access.");
  expect(screen.queryByText("Previous organization")).not.toBeInTheDocument();
  state.pickerStatus = 200;
  await userEvent.click(screen.getByRole("button", { name: "Reload assignment" }));
  expect(await screen.findByRole("combobox")).toHaveValue("");
  expect(screen.getByText("Previous organization")).toBeInTheDocument();
});

it("loads a Beijing window by request admission and preserves exact charges and unsettled states", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailOrganizationConsumptionPanel />, {
    searchParams: { organization: org.id, start: "2026-11-01T00:00:00Z", end: "2026-11-04T00:00:00Z" },
  });
  expect(await screen.findByText("$9,007,199,254,740,993.000000001")).toBeInTheDocument();
  const table = screen.getByRole("table");
  expect(within(table).getByText("$0.000000001")).toBeInTheDocument();
  expect(within(table).getAllByText("Not finalized")).toHaveLength(4);
  expect(within(table).getByText("Unknown")).toBeInTheDocument();
  expect(within(table).getByText("Conflict")).toBeInTheDocument();
  const read = calls.find((call) => call.url.pathname.endsWith("/consumption"));
  expect(read?.url.searchParams.get("start")).toBe("2026-11-01T00:00:00.000Z");
  expect(read?.url.searchParams.get("end")).toBe("2026-11-04T00:00:00.000Z");
  expect(screen.getByLabelText("Accepted from (Beijing)")).toHaveValue("2026-11-01T08:00");
  await userEvent.click(
    within(screen.getByRole("region", { name: "Organization requests" })).getByRole("button", { name: "Next page" }),
  );
  await waitFor(() =>
    expect(
      calls
        .filter((call) => call.url.pathname.endsWith("/consumption"))
        .at(-1)
        ?.url.searchParams.get("offset"),
    ).toBe("25"),
  );
});

it("persists applied dates and resets request pagination without changing unrelated URL state", async () => {
  const { calls } = serve();
  const onUrlUpdate = vi.fn();
  renderWithProviders(<RetailOrganizationConsumptionPanel />, {
    searchParams: {
      organization: org.id,
      start: "2026-11-01T00:00:00Z",
      end: "2026-11-04T00:00:00Z",
      page: "2",
      unrelated: "keep",
    },
    onUrlUpdate,
  });
  await screen.findByRole("table");
  fireEvent.change(screen.getByLabelText("Accepted from (Beijing)"), { target: { value: "2026-11-02T00:00" } });
  fireEvent.change(screen.getByLabelText("Accepted before (Beijing)"), { target: { value: "2026-11-03T00:00" } });
  await userEvent.click(screen.getByRole("button", { name: "Apply window" }));
  await waitFor(() =>
    expect(
      calls
        .filter((call) => call.url.pathname.endsWith("/consumption"))
        .at(-1)
        ?.url.searchParams.get("start"),
    ).toBe("2026-11-01T16:00:00.000Z"),
  );
  expect(onUrlUpdate.mock.lastCall?.[0].queryString).toContain("unrelated=keep");
  expect(onUrlUpdate.mock.lastCall?.[0].queryString).not.toContain("page=2");
});

it("blocks invalid URL windows before the consumption request", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailOrganizationConsumptionPanel />, {
    searchParams: { organization: org.id, start: "2026-11-01T00:00:00Z" },
  });
  expect(await screen.findByText("Choose both a start and an end time.")).toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.endsWith("/consumption"))).toBe(false);
});

it("hides a loaded report after a failed authority refresh", async () => {
  const { state } = serve();
  renderWithProviders(<RetailOrganizationConsumptionPanel />, { searchParams: { organization: org.id } });
  await screen.findByRole("table");
  state.readStatus = 403;
  await userEvent.click(screen.getByRole("button", { name: "Reload preview" }));
  await screen.findByText("Organization finance access changed. Reload to check your current access.");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("$9,007,199,254,740,993.000000001")).not.toBeInTheDocument();
});

it("removes cached finance results immediately when the signed-in role loses proxy admin access", async () => {
  const { calls } = serve();
  const view = renderWithProviders(<RetailOrganizationConsumptionPanel />, { searchParams: { organization: org.id } });
  await screen.findByRole("table");
  const before = calls.length;
  auth.userRole = "Org Admin";
  view.rerender(<RetailOrganizationConsumptionPanel />);
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("$9,007,199,254,740,993.000000001")).not.toBeInTheDocument();
  expect(calls).toHaveLength(before);
});

it("shows safe guidance for an oversized cohort instead of a partial charge total", async () => {
  const { state } = serve();
  state.readStatus = 422;
  renderWithProviders(<RetailOrganizationConsumptionPanel />, { searchParams: { organization: org.id } });
  expect(await screen.findByRole("alert")).toHaveTextContent("at most 10,000 requests");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("private-source-marker")).not.toBeInTheDocument();
});

it.each([true, false])("links finance from account and organization rows only when enabled=%s", async (enabled) => {
  const { state } = serve();
  state.enabled = enabled;
  renderWithProviders(
    <>
      <AdminWalletsPanel />
      <RetailOrganizationsPanel />
    </>,
  );
  await screen.findByText("customer-one");
  await screen.findByText(org.name);
  if (enabled) {
    expect(screen.getByRole("button", { name: "Billing organization for customer customer-one" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Consumption" })).toHaveAttribute(
      "href",
      "/ui/organization-consumption?organization=org-one",
    );
  } else {
    expect(
      screen.queryByRole("button", { name: "Billing organization for customer customer-one" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Consumption" })).not.toBeInTheDocument();
  }
});

it.each([
  { userRole: "Internal User", isViewOnly: false },
  { userRole: "Org Admin", isViewOnly: false },
  { userRole: "proxy_admin_viewer", isViewOnly: false },
  { userRole: "Admin", isViewOnly: true },
])("does not expose account or organization finance entry points to %o", async (role) => {
  const { calls } = serve();
  Object.assign(auth, role);
  renderWithProviders(
    <>
      <AdminWalletsPanel />
      <RetailOrganizationsPanel />
    </>,
  );
  await screen.findByText(org.name);
  expect(
    screen.queryByRole("button", { name: "Billing organization for customer customer-one" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Consumption" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
});
