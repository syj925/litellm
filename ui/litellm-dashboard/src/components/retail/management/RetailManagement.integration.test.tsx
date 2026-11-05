import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import RetailOperationAuditPanel from "../audit/RetailOperationAuditPanel";
import RetailOrganizationsPanel from "../organizations/RetailOrganizationsPanel";
import type { RetailOrganization } from "../organizations/types";
import RetailReportsPanel from "../reports/RetailReportsPanel";
import type { RetailReport } from "../reports/types";

const auth = vi.hoisted(() => ({ userRole: "Admin", isViewOnly: false }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ ...auth, accessToken: "test-session", userId: "actor-one", isAuthorized: true, isLoading: false }),
}));

const organization: RetailOrganization = {
  id: "organization-one",
  name: "Example organization",
  enabled: true,
  version: 3,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  member_role: null,
  can_edit: true,
  can_associate_members: true,
  can_manage_members: true,
  can_view_events: true,
};
const report: RetailReport = {
  id: "report-one",
  account_id: "actor-one",
  scope: "account",
  state: "queued",
  start_at: "2026-10-01T00:00:00Z",
  end_at: "2026-10-02T00:00:00Z",
  snapshot_at: "2026-10-03T00:00:00Z",
  created_at: "2026-10-03T00:00:00Z",
  expires_at: "2026-10-04T00:00:00Z",
  partial: false,
  currency: "USD",
  timezone: "Asia/Shanghai",
  total_rows: 0,
  totals: { credit_nanos: "0", charge_nanos: "0", release_nanos: "0", credit: "0", charge: "0", release: "0" },
  unresolved: { pending: 0, unknown: 0, conflict: 0 },
};
interface RequestRecord {
  url: URL;
  method: string;
  body: unknown;
  headers: Headers;
}
function page<T>(items: T[]) {
  return { items, total: items.length, limit: 25, offset: 0 };
}
function response(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}
function serve(handler: (request: RequestRecord) => Response | Promise<Response>, enabled = true) {
  const calls: RequestRecord[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const request = {
        url: new URL(input),
        method: options?.method ?? "GET",
        body: options?.body ? (JSON.parse(String(options.body)) as unknown) : null,
        headers: new Headers(options?.headers),
      };
      calls.push(request);
      if (request.url.pathname === "/retail/config")
        return response({
          organization_management_enabled: enabled,
          operation_audit_enabled: enabled,
          report_export_enabled: enabled,
        });
      return handler(request);
    }),
  );
  return calls;
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

it("keeps disabled organization features off native enterprise endpoints", async () => {
  const calls = serve(() => response({ detail: "Unexpected request" }, 500), false);
  renderWithProviders(<RetailOrganizationsPanel />);
  expect(await screen.findByText("Organization management is not enabled")).toBeInTheDocument();
  expect(calls.map((call) => call.url.pathname)).toEqual(["/retail/config"]);
});

it("creates organizations from existing account IDs with an idempotency key", async () => {
  const calls = serve((request) =>
    request.method === "POST"
      ? response({ ...organization, name: "New organization" })
      : response(page([organization])),
  );
  renderWithProviders(<RetailOrganizationsPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Create organization" }));
  const dialog = await screen.findByRole("dialog", { name: "Create organization" });
  fireEvent.change(within(dialog).getByLabelText("Organization name"), { target: { value: " New organization " } });
  fireEvent.change(within(dialog).getByLabelText("Existing admin account ID"), { target: { value: "known-account" } });
  await user.click(within(dialog).getByRole("button", { name: "Save organization" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "POST")).toHaveLength(1));
  const write = calls.find((call) => call.method === "POST");
  expect(write?.url.pathname).toBe("/retail/admin/organizations");
  expect(write?.body).toEqual({ name: "New organization", admin_user_id: "known-account" });
  expect(write?.headers.get("Idempotency-Key")).toBeTruthy();
});

it("leaves stale organization edits blocked instead of overwriting a newer version", async () => {
  const calls = serve((request) =>
    request.method === "PATCH" ? response({ detail: "Version conflict" }, 409) : response(page([organization])),
  );
  renderWithProviders(<RetailOrganizationsPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit organization Example organization" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit organization" });
  fireEvent.change(within(dialog).getByLabelText("Organization name"), { target: { value: "Changed name" } });
  await user.click(within(dialog).getByRole("button", { name: "Save organization" }));
  expect(await screen.findByText("Organization changed")).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Save organization" })).toBeDisabled();
  expect(calls.filter((call) => call.method === "PATCH").map((call) => call.body)).toEqual([
    { name: "Changed name", enabled: true, expected_version: 3 },
  ]);
});

it("does not fetch member rosters or admin endpoints for an ordinary member", async () => {
  auth.userRole = "Internal User";
  const ordinary = {
    ...organization,
    member_role: "member" as const,
    can_edit: false,
    can_associate_members: false,
    can_manage_members: false,
    can_view_events: false,
  };
  const calls = serve(() => response(page([ordinary])));
  renderWithProviders(<RetailOrganizationsPanel />);
  expect(await screen.findByText("Example organization")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /View members/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Create organization" })).not.toBeInTheDocument();
  expect(calls.map((call) => call.url.pathname)).toEqual(["/retail/config", "/retail/organizations"]);
});

it("removes cached organization rows and open editors when a refresh revokes access", async () => {
  let revoked = false;
  serve(() => (revoked ? response({ detail: "Access revoked" }, 403) : response(page([organization]))));
  renderWithProviders(<RetailOrganizationsPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit organization Example organization" }));
  expect(await screen.findByRole("dialog", { name: "Edit organization" })).toBeInTheDocument();
  revoked = true;
  await testQueryClient.invalidateQueries({ queryKey: ["retail", "organizations"] });
  expect(await screen.findByText("Unable to load organizations")).toBeInTheDocument();
  expect(screen.queryByText("Example organization")).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "Edit organization" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Create organization" })).not.toBeInTheDocument();
});

it("lets delegated organization admins toggle approved members without role elevation", async () => {
  auth.userRole = "Internal User";
  const delegated = { ...organization, member_role: "admin" as const, can_edit: false, can_associate_members: false };
  const member = {
    user_id: "approved-account",
    role: "member",
    enabled: true,
    version: 4,
    created_at: organization.created_at,
    updated_at: organization.updated_at,
  };
  const calls = serve((request) => {
    if (request.method === "PATCH")
      return response({
        organization: { ...delegated, version: 4 },
        member: { ...member, enabled: false, version: 5 },
      });
    if (request.url.pathname.endsWith("/members")) return response({ ...page([member]), organization: delegated });
    return response(page([delegated]));
  });
  renderWithProviders(<RetailOrganizationsPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View members of Example organization" }));
  await user.click(await screen.findByRole("button", { name: "Edit member approved-account" }));
  const dialog = await screen.findByRole("dialog", { name: "Edit member" });
  expect(within(dialog).queryByLabelText("Organization role")).not.toBeInTheDocument();
  await user.click(within(dialog).getByRole("checkbox", { name: "Membership enabled" }));
  await user.click(within(dialog).getByRole("button", { name: "Save member" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(1));
  const write = calls.find((call) => call.method === "PATCH");
  const expectedMembership = { role: "member", enabled: false, expected_version: 4, expected_organization_version: 3 };
  expect(write?.body).toEqual(expectedMembership);
  expect(write?.headers.get("Idempotency-Key")).toBeTruthy();
  expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
});

it.each([
  { role: "Internal User", viewer: false },
  { role: "Admin", viewer: true },
])(
  "uses only authorized audit scopes for $role (viewer=$viewer) and strips actor filters",
  async ({ role, viewer }) => {
    auth.userRole = role;
    auth.isViewOnly = viewer;
    const delegated = { ...organization, member_role: "admin" as const, can_edit: false, can_associate_members: false };
    const calls = serve((request) =>
      request.url.pathname.endsWith("operation-events") ? response(page([])) : response(page([delegated])),
    );
    renderWithProviders(<RetailOperationAuditPanel />, {
      searchParams: { organization: organization.id, actor: "foreign-actor" },
    });
    expect(await screen.findByText("No operation events found")).toBeInTheDocument();
    expect(screen.queryByLabelText("Actor account ID")).not.toBeInTheDocument();
    const audit = calls.find((call) => call.url.pathname.endsWith("operation-events"));
    expect(audit?.url.pathname).toBe(`/retail/organizations/${organization.id}/operation-events`);
    expect(audit?.url.searchParams.has("actor_id")).toBe(false);
    expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
  },
);

it("does not probe foreign audit organizations from a deep link", async () => {
  auth.userRole = "Internal User";
  const calls = serve(() => response(page([{ ...organization, can_edit: false, can_associate_members: false }])));
  renderWithProviders(<RetailOperationAuditPanel />, { searchParams: { organization: "foreign-organization" } });
  expect(await screen.findByText("Choose an authorized organization")).toBeInTheDocument();
  expect(calls.map((call) => call.url.pathname)).toEqual(["/retail/config", "/retail/organizations"]);
});

const addedAuditActions = [
  ["monthly_budget.created", "Monthly budget plan created"],
  ["monthly_budget.updated", "Monthly budget plan updated"],
  ["monthly_budget.stopped", "Monthly budget plan stopped"],
  ["billing_organization.updated", "Billing organization updated"],
  ["organization_report.created", "Organization report requested"],
  ["organization_report.completed", "Organization report completed"],
  ["organization_report.failed", "Organization report failed"],
  ["finance_grant.updated", "Finance access updated"],
  ["delegated_report.created", "Delegated finance report requested"],
  ["delegated_report.completed", "Delegated finance report completed"],
  ["delegated_report.failed", "Delegated finance report failed"],
  ["native_key.rotated", "API key rotated"],
  ["native_password.changed", "Account password changed"],
  ["native_session.revoked", "Account sessions revoked"],
  ["native_identity.configured", "Organization sign-in bindings configured"],
  ["native_identity.signed_in", "Organization sign-in completed"],
  ["native_api_identity.configured", "API access-token bindings configured"],
  ["native_scim.configured", "Directory provisioning configured"],
  ["native_scim.user_created", "Directory user created"],
  ["native_scim.user_updated", "Directory user updated"],
  ["native_scim.user_deleted", "Directory user removed"],
  ["native_scim.group_created", "Directory group created"],
  ["native_scim.group_updated", "Directory group updated"],
  ["native_scim.group_deleted", "Directory group removed"],
  ["native_request_policy.prompt_created", "Saved prompt created"],
  ["native_request_policy.prompt_updated", "Saved prompt updated"],
  ["native_request_policy.prompt_deleted", "Saved prompt deleted"],
  ["native_request_policy.policy_created", "Request policy created"],
  ["native_request_policy.policy_updated", "Request policy updated"],
  ["native_request_policy.policy_deleted", "Request policy deleted"],
  ["native_request_policy.binding_created", "Policy binding created"],
  ["native_request_policy.binding_updated", "Policy binding updated"],
  ["native_request_policy.binding_deleted", "Policy binding deleted"],
] as const;

it.each(addedAuditActions)("filters and labels the controlled admin audit action %s", async (action, label) => {
  const calls = serve((request) =>
    response(
      page(
        request.url.searchParams.get("action") === action
          ? [
              {
                id: "audit-event",
                organization_id: null,
                actor_id: "actor-one",
                action,
                resource_type: "organization",
                resource_id: "organization-one",
                result: "succeeded",
                version_before: 1,
                version_after: 2,
                created_at: "2026-11-01T00:00:00Z",
                changes: {},
              },
            ]
          : [],
      ),
    ),
  );
  renderWithProviders(<RetailOperationAuditPanel />, { searchParams: { page: "3" } });
  const select = await screen.findByRole("combobox", { name: "Action" });
  const values = within(select)
    .getAllByRole("option")
    .map((option) => (option as HTMLOptionElement).value);
  expect(values).toEqual([
    "",
    "organization.created",
    "organization.updated",
    "organization.member_associated",
    "organization.member_updated",
    "report.created",
    "report.completed",
    "report.failed",
    "object_metadata.updated",
    "model_budget.created",
    "model_budget.updated",
    "model_budget.scheduled",
    ...addedAuditActions.map(([value]) => value),
    "audit_delivery.updated",
    "audit_delivery.retried",
    "audit_delivery.discarded",
  ]);
  fireEvent.change(select, { target: { value: action } });
  await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
  const events = screen.getByRole("region", { name: "Operation events" });
  expect(await within(events).findByText(label)).toBeInTheDocument();
  const filtered = calls.find((call) => call.url.searchParams.get("action") === action);
  expect(filtered?.url.pathname).toBe("/retail/admin/operation-events");
  expect(filtered?.url.searchParams.get("offset")).toBe("0");
});

it.each([
  { role: "Internal User", viewer: false, action: "finance_grant.updated" },
  ...[
    "prompt_created",
    "prompt_updated",
    "prompt_deleted",
    "policy_created",
    "policy_updated",
    "policy_deleted",
    "binding_created",
    "binding_updated",
    "binding_deleted",
  ].flatMap((name) => [
    { role: "Internal User", viewer: false, action: `native_request_policy.${name}` },
    { role: "Admin", viewer: true, action: `native_request_policy.${name}` },
  ]),
  { role: "Admin", viewer: true, action: "finance_grant.updated" },
  { role: "Internal User", viewer: false, action: "native_identity.configured" },
  { role: "Admin", viewer: true, action: "native_identity.configured" },
  { role: "Internal User", viewer: false, action: "native_identity.signed_in" },
  { role: "Admin", viewer: true, action: "native_identity.signed_in" },
  { role: "Internal User", viewer: false, action: "native_api_identity.configured" },
  { role: "Admin", viewer: true, action: "native_api_identity.configured" },
  { role: "Internal User", viewer: false, action: "native_scim.configured" },
  { role: "Admin", viewer: true, action: "native_scim.configured" },
  { role: "Internal User", viewer: false, action: "native_scim.user_created" },
  { role: "Admin", viewer: true, action: "native_scim.user_created" },
  { role: "Internal User", viewer: false, action: "native_scim.user_updated" },
  { role: "Admin", viewer: true, action: "native_scim.user_updated" },
  { role: "Internal User", viewer: false, action: "native_scim.user_deleted" },
  { role: "Admin", viewer: true, action: "native_scim.user_deleted" },
  { role: "Internal User", viewer: false, action: "native_scim.group_created" },
  { role: "Admin", viewer: true, action: "native_scim.group_created" },
  { role: "Internal User", viewer: false, action: "native_scim.group_updated" },
  { role: "Admin", viewer: true, action: "native_scim.group_updated" },
  { role: "Internal User", viewer: false, action: "native_scim.group_deleted" },
  { role: "Admin", viewer: true, action: "native_scim.group_deleted" },
])(
  "strips $action and limits audit actions to organizations for $role (viewer=$viewer)",
  async ({ role, viewer, action }) => {
    auth.userRole = role;
    auth.isViewOnly = viewer;
    const calls = serve((request) =>
      response(page(request.url.pathname.endsWith("operation-events") ? [] : [organization])),
    );
    renderWithProviders(<RetailOperationAuditPanel />, {
      searchParams: { organization: organization.id, action },
    });
    await screen.findByText("No operation events found");
    const select = screen.getByRole("combobox", { name: "Action" });
    expect(select).toHaveValue("");
    expect(
      within(select)
        .getAllByRole("option")
        .map((option) => (option as HTMLOptionElement).value),
    ).toEqual([
      "",
      "organization.created",
      "organization.updated",
      "organization.member_associated",
      "organization.member_updated",
    ]);
    expect(calls.find((call) => call.url.pathname.endsWith("operation-events"))?.url.searchParams.has("action")).toBe(
      false,
    );
    fireEvent.change(select, { target: { value: "organization.member_updated" } });
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() =>
      expect(calls.some((call) => call.url.searchParams.get("action") === "organization.member_updated")).toBe(true),
    );
    expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
  },
);

it("ignores unrecognized audit deep-link actions instead of sending unsupported filter values", async () => {
  const calls = serve(() => response(page([])));
  renderWithProviders(<RetailOperationAuditPanel />, { searchParams: { action: "unsupported.action" } });
  await screen.findByText("No operation events found");
  expect(screen.getByRole("combobox", { name: "Action" })).toHaveValue("");
  expect(calls.find((call) => call.url.pathname.endsWith("operation-events"))?.url.searchParams.has("action")).toBe(
    false,
  );
});

it("creates personal reports without account overrides and reuses the key after a failed retry", async () => {
  auth.userRole = "Internal User";
  let writes = 0;
  const calls = serve((request) => {
    if (request.method === "POST") {
      writes += 1;
      return writes === 1 ? response({ detail: "Temporary failure" }, 503) : response(report);
    }
    if (request.url.pathname.endsWith(report.id)) return response(report);
    return response(page([]));
  });
  renderWithProviders(<RetailReportsPanel />);
  await screen.findByRole("heading", { name: "Create a wallet report" });
  expect(screen.queryByLabelText("Account ID (optional)")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("From (Beijing time)"), { target: { value: "2026-10-01T00:00" } });
  fireEvent.change(screen.getByLabelText("Until (exclusive, Beijing time)"), { target: { value: "2026-10-02T00:00" } });
  fireEvent.change(screen.getByLabelText("Transaction type"), { target: { value: "release" } });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Create report" }));
  expect(await screen.findByText("Temporary failure")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Create report" }));
  expect(await screen.findByRole("dialog", { name: "Wallet report details" })).toBeInTheDocument();
  const creates = calls.filter((call) => call.method === "POST");
  expect(creates.map((call) => call.body)).toEqual(
    Array.from({ length: 2 }, () => ({
      start_at: "2026-09-30T16:00:00.000Z",
      end_at: "2026-10-01T16:00:00.000Z",
      currency: "USD",
      timezone: "Asia/Shanghai",
      kind: "release",
    })),
  );
  expect(creates[0].headers.get("Idempotency-Key")).toBeTruthy();
  expect(creates[1].headers.get("Idempotency-Key")).toBe(creates[0].headers.get("Idempotency-Key"));
});

it("keeps queued artifacts unavailable and downloads ready artifacts with fresh authentication", async () => {
  let ready = false;
  const calls = serve((request) =>
    request.url.pathname.endsWith("/download")
      ? response({ detail: "Access revoked" }, 403)
      : response(page([{ ...report, state: ready ? "ready" : "queued" }])),
  );
  renderWithProviders(<RetailReportsPanel />);
  const download = await screen.findByRole("button", { name: "Download report report-one" });
  expect(download).toBeDisabled();
  ready = true;
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Refresh reports" }));
  await waitFor(() => expect(download).toBeEnabled());
  await user.click(download);
  expect(await screen.findByText("Unable to download report")).toBeInTheDocument();
  const artifact = calls.find((call) => call.url.pathname.endsWith("/download"));
  expect(artifact?.headers.get("Authorization")).toBe("Bearer test-session");
  expect(artifact?.url.search).toBe("");
});

it("keeps native viewer accounts on own report lists without export creation controls", async () => {
  auth.userRole = "Admin";
  auth.isViewOnly = true;
  const calls = serve(() => response(page([{ ...report, state: "ready" }])));
  renderWithProviders(<RetailReportsPanel />);
  expect(await screen.findByText(/Your account has read-only access/)).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Create a wallet report" })).not.toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Download report report-one" })).toBeEnabled();
  expect(calls.map((call) => call.url.pathname)).toEqual(["/retail/config", "/retail/reports"]);
});

it("removes cached audit details after a permission revocation", async () => {
  let revoked = false;
  const event = {
    id: "event-one",
    organization_id: organization.id,
    actor_id: "operator-one",
    action: "organization.updated",
    resource_type: "organization",
    resource_id: organization.id,
    result: "succeeded",
    version_before: 2,
    version_after: 3,
    created_at: organization.updated_at,
    changes: { name: "Example organization" },
  };
  serve(() => (revoked ? response({ detail: "Access revoked" }, 403) : response(page([event]))));
  renderWithProviders(<RetailOperationAuditPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View event event-one" }));
  expect(await screen.findByRole("dialog", { name: "Operation event" })).toBeInTheDocument();
  revoked = true;
  await testQueryClient.invalidateQueries({ queryKey: ["retail", "operation-events"] });
  expect(await screen.findByText("Unable to load operation events")).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "Operation event" })).not.toBeInTheDocument();
  expect(screen.queryByText("operator-one")).not.toBeInTheDocument();
});
