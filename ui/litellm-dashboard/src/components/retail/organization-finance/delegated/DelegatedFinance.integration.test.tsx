import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../../tests/test-utils";
import OrganizationMembers from "../../organizations/OrganizationMembers";
import RetailOrganizationsPanel from "../../organizations/RetailOrganizationsPanel";
import type { RetailOrganization, RetailOrganizationMember } from "../../organizations/types";
import DelegatedFinancePanel from "./DelegatedFinancePanel";
import FinanceGrantDialog from "./FinanceGrantDialog";
import type { DelegatedConsumption, DelegatedReport, FinanceGrant, GrantUpdate } from "./types";

const auth = vi.hoisted(() => ({
  userRole: "Internal User",
  isViewOnly: false,
  isAuthorized: true,
  accessToken: "session-one",
}));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ ...auth, userId: "reader-one", isLoading: false }),
}));
const revisionOne = "8c148e29-8d9a-4cea-a152-d75831bcbf7b";
const revisionTwo = "48c40d7a-2ccf-4af4-9925-b61f74386452";
const organization: RetailOrganization = {
  id: "org-one",
  name: "Authorized organization",
  enabled: true,
  version: 1,
  member_role: "admin",
  can_edit: false,
  can_associate_members: false,
  can_manage_members: true,
  can_view_events: true,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
};
const member: RetailOrganizationMember = {
  user_id: "customer-admin",
  role: "admin",
  enabled: true,
  has_finance_grant: true,
  version: 3,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
};
const grant: FinanceGrant = {
  organization_id: organization.id,
  user_id: member.user_id,
  grant_id: null,
  version: 0,
  revision: null,
  can_view: false,
  can_export: false,
  effective: false,
  state: "unassigned",
  updated_at: null,
  updated_by: null,
};
const summary = {
  accepted_requests: 3,
  settled_requests: 1,
  released_requests: 0,
  pending_requests: 1,
  unknown_requests: 1,
  conflict_requests: 0,
  settled_usd: "1.000000001",
  settled_nanos: "1000000001",
};
const usage: DelegatedConsumption = {
  organization_id: organization.id,
  organization_name: organization.name,
  axis: "admission",
  access_revision: revisionOne,
  snapshot_at: "2026-11-04T00:00:00Z",
  start: "2026-11-01T00:00:00Z",
  end: "2026-11-02T00:00:00Z",
  partial: false,
  live: true,
  max_requests: 10000,
  total: 3,
  limit: 25,
  offset: 0,
  summary,
  items: [
    {
      sequence: 1,
      model: "public/alpha",
      captured_at: "2026-11-01T00:00:00Z",
      state: "settled",
      settled_usd: summary.settled_usd,
    },
    { sequence: 2, model: "public/alpha", captured_at: "2026-11-01T00:00:01Z", state: "unknown", settled_usd: null },
  ],
};
const job: DelegatedReport = {
  id: "delegated-one",
  scope: "delegated_organization",
  axis: "admission",
  organization_id: organization.id,
  organization_name: organization.name,
  access_revision: revisionOne,
  state: "ready",
  start_at: usage.start,
  end_at: usage.end,
  snapshot_at: usage.snapshot_at,
  created_at: usage.snapshot_at,
  expires_at: "2026-11-06T00:00:00Z",
  partial: false,
  total_rows: 3,
  summary,
  artifact_bytes: 100,
};
type Call = { url: URL; method: string; body: unknown; headers: Headers; cache: RequestCache | undefined };
type Server = {
  member: RetailOrganizationMember;
  enabled: boolean;
  exports: boolean;
  canView: boolean;
  canExport: boolean;
  revision: string;
  grant: FinanceGrant;
  writeStatus: number;
  grantReadStatus: number;
  accessStatus: number;
  liveStatus: number;
  reportStatus: number;
  networkFailures: number;
  jobs: DelegatedReport[];
  amount: string;
};
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function page<T>(items: T[]) {
  return { items, total: items.length, limit: 25, offset: 0 };
}
function grantEndpoint(call: Call, state: Server) {
  if (call.method === "GET")
    return json(
      state.grantReadStatus === 200 ? state.grant : { detail: "private-source-marker" },
      state.grantReadStatus,
    );
  if (state.networkFailures > 0) {
    state.networkFailures -= 1;
    throw new TypeError("Failed to fetch");
  }
  if (state.writeStatus !== 200) return json({ detail: "private-source-marker" }, state.writeStatus);
  const body = call.body as GrantUpdate;
  state.grant = {
    ...state.grant,
    ...body,
    version: body.expected_version + 1,
    effective: body.can_view,
    state: body.can_view ? "active" : "revoked",
  };
  return json(state.grant);
}
function reportEndpoint(call: Call, state: Server) {
  if (state.reportStatus !== 200) return json({ detail: "private-source-marker" }, state.reportStatus);
  if (call.url.pathname.endsWith("/download"))
    return new Response("sequence,model,state\n1,public/alpha,settled\n", { headers: { "Content-Type": "text/csv" } });
  if (call.method === "POST") {
    if (state.networkFailures > 0) {
      state.networkFailures -= 1;
      throw new TypeError("Failed to fetch");
    }
    const created = { ...job, id: "created-authorized", state: "queued" as const, access_revision: state.revision };
    state.jobs = [created, ...state.jobs];
    return json(created, 202);
  }
  if (call.url.pathname.endsWith("/reports"))
    return json(page(state.jobs.filter((item) => item.access_revision === state.revision)));
  return json(state.jobs.find((item) => item.id === call.url.pathname.split("/").at(-1)) ?? job);
}
function serve() {
  const calls: Call[] = [];
  const state: Server = {
    member: structuredClone(member),
    enabled: true,
    exports: true,
    canView: true,
    canExport: false,
    revision: revisionOne,
    grant: structuredClone(grant),
    writeStatus: 200,
    grantReadStatus: 200,
    accessStatus: 200,
    liveStatus: 200,
    reportStatus: 200,
    networkFailures: 0,
    jobs: [structuredClone(job)],
    amount: summary.settled_usd,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const call: Call = {
        url: new URL(input),
        method: options?.method ?? "GET",
        body: options?.body ? (JSON.parse(String(options.body)) as unknown) : null,
        headers: new Headers(options?.headers),
        cache: options?.cache,
      };
      calls.push(call);
      const path = call.url.pathname;
      if (path === "/retail/config") {
        const config = {
          organization_management_enabled: true,
          org_finance_enabled: true,
          org_finance_delegation_enabled: state.enabled,
          report_export_enabled: state.exports,
        };
        return json(config);
      }
      if (path.includes("/finance-grants/")) return grantEndpoint(call, state);
      if (path.endsWith("/members")) return json({ ...page([state.member]), organization });
      if (["/retail/organizations", "/retail/admin/organizations"].includes(path)) return json(page([organization]));
      if (path === "/retail/organization-finance/organizations")
        return json(
          page(
            state.canView
              ? [
                  {
                    id: organization.id,
                    name: organization.name,
                    can_view: true,
                    can_export: state.canExport,
                    access_revision: state.revision,
                  },
                ]
              : [],
          ),
        );
      if (path.endsWith("/finance/access"))
        return json(
          state.accessStatus === 200
            ? {
                organization_id: organization.id,
                organization_name: organization.name,
                can_view: state.canView,
                can_export: state.canExport,
                access_revision: state.canView ? state.revision : null,
              }
            : { detail: "private-source-marker" },
          state.accessStatus,
        );
      if (path.endsWith("/finance/consumption"))
        return json(
          state.liveStatus === 200
            ? { ...usage, access_revision: state.revision, summary: { ...summary, settled_usd: state.amount } }
            : { detail: "private-source-marker" },
          state.liveStatus,
        );
      if (path.includes("/finance/reports")) return reportEndpoint(call, state);
      throw new Error(`Unexpected request ${path}`);
    }),
  );
  return { calls, state };
}
function renderFinance() {
  return renderWithProviders(<DelegatedFinancePanel />, {
    searchParams: { organization: organization.id, start: usage.start, end: usage.end },
  });
}
function grantForm() {
  return renderWithProviders(
    <FinanceGrantDialog organization={organization} member={member} onClose={vi.fn()} onSaved={vi.fn()} />,
  );
}
beforeEach(() => {
  const initial = { userRole: "Internal User", isViewOnly: false, isAuthorized: true, accessToken: "session-one" };
  Object.assign(auth, initial);
  testQueryClient.clear();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  testQueryClient.clear();
});

it.each([
  { userRole: "Admin", isViewOnly: true, isAuthorized: true },
  { userRole: "proxy_admin_viewer", isViewOnly: false, isAuthorized: true },
  { userRole: "Internal User", isViewOnly: false, isAuthorized: false },
])("does not load delegated finance for readonly or unauthenticated sessions %o", (role) => {
  const { calls } = serve();
  Object.assign(auth, role);
  renderFinance();
  expect(calls).toHaveLength(0);
});
it("hides the new feature and performs no finance request when delegation is disabled", async () => {
  const { calls, state } = serve();
  state.enabled = false;
  renderFinance();
  await screen.findByText("Organization finance delegation is not enabled.");
  expect(calls.every((call) => call.url.pathname === "/retail/config")).toBe(true);
});
it("does not grant financial visibility from the native Org Admin role alone", async () => {
  const { calls, state } = serve();
  state.canView = false;
  auth.userRole = "Org Admin";
  renderFinance();
  await screen.findByText("Your account has no finance view grant for this organization.");
  expect(calls.some((call) => /consumption|reports/.test(call.url.pathname))).toBe(false);
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});
it("allows an explicitly granted retail admin member whose native session is Internal User", async () => {
  const { calls } = serve();
  renderFinance();
  const region = await screen.findByRole("region", { name: "Authorized organization usage" });
  expect(within(region).getAllByText("public/alpha")).toHaveLength(2);
  expect(within(region).getByText("Not finalized")).toBeInTheDocument();
  expect(within(region).queryByText("Request / account")).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/admin/"))).toBe(false);
  expect(calls.some((call) => call.url.pathname.includes("/reports"))).toBe(false);
});
it("preserves view access when the gateway disables fixed report exports", async () => {
  const { state, calls } = serve();
  state.canExport = true;
  state.exports = false;
  renderFinance();
  await screen.findByRole("region", { name: "Authorized organization usage" });
  expect(screen.queryByRole("region", { name: "Authorized finance exports" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/reports"))).toBe(false);
});

it("downloads a safe projected CSV with the fresh bearer token and no-store handling", async () => {
  const { state, calls } = serve();
  state.canExport = true;
  const NativeURL = URL;
  vi.stubGlobal(
    "URL",
    class extends NativeURL {
      static createObjectURL = vi.fn(() => "blob:delegated-csv");
      static revokeObjectURL = vi.fn();
    },
  );
  const links: HTMLAnchorElement[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    links.push(this);
  });
  const view = renderFinance();
  await screen.findByRole("button", { name: "Download authorized snapshot delegated-one" });
  auth.accessToken = "fresh-token";
  view.rerender(<DelegatedFinancePanel />);
  await userEvent.click(screen.getByRole("button", { name: "Download authorized snapshot delegated-one" }));
  await waitFor(() => expect(links).toHaveLength(1));
  const request = calls.find((call) => call.url.pathname.endsWith("/download"));
  expect(request?.url.pathname).toBe("/retail/organizations/org-one/finance/reports/delegated-one/download");
  expect(request?.url.search).toBe("");
  expect(request?.cache).toBe("no-store");
  expect(request?.headers.get("Authorization")).toBe("Bearer fresh-token");
  expect(links[0].download).toBe("organization-finance-delegated-one.csv");
});

it("removes all historical finance data on view loss and fetches new data after reauthorization", async () => {
  const { state, calls } = serve();
  state.canExport = true;
  renderFinance();
  await screen.findByRole("button", { name: "Download authorized snapshot delegated-one" });
  state.canView = false;
  await userEvent.click(screen.getByRole("button", { name: "Reload access" }));
  await screen.findByText("Your account has no finance view grant for this organization.");
  expect(screen.queryByRole("region", { name: "Authorized organization usage" })).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Authorized finance exports" })).not.toBeInTheDocument();
  state.canView = true;
  state.revision = revisionTwo;
  state.amount = "9.123456789";
  await userEvent.click(screen.getByRole("button", { name: "Reload access" }));
  await screen.findByText("$9.123456789");
  await screen.findByText("No snapshots in your current authorization.");
  expect(screen.queryByRole("button", { name: "Download authorized snapshot delegated-one" })).not.toBeInTheDocument();
  expect(calls.filter((call) => call.url.pathname.endsWith("/finance/consumption"))).toHaveLength(2);
});
it("removes only exports when export permission is lost during download", async () => {
  const { state } = serve();
  state.canExport = true;
  renderFinance();
  const download = await screen.findByRole("button", { name: "Download authorized snapshot delegated-one" });
  state.reportStatus = 403;
  state.canExport = false;
  await userEvent.click(download);
  await waitFor(() =>
    expect(screen.queryByRole("region", { name: "Authorized finance exports" })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("region", { name: "Authorized organization usage" })).toBeInTheDocument();
});
it("hides projected usage on an authority failure and requires capability reload", async () => {
  const { state } = serve();
  state.liveStatus = 403;
  renderFinance();
  await screen.findByText("Finance access changed. Reload access before viewing more data.");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("private-source-marker")).not.toBeInTheDocument();
  state.liveStatus = 200;
  await userEvent.click(screen.getByRole("button", { name: "Reload access" }));
  await screen.findByRole("region", { name: "Authorized organization usage" });
});
it("creates only an applied safe window and reuses the key after an uncertain result", async () => {
  const { state, calls } = serve();
  state.canExport = true;
  state.networkFailures = 1;
  renderFinance();
  const create = await screen.findByRole("button", { name: "Create authorized CSV" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Accepted from (Beijing)"), { target: { value: "2026-10-01T00:00" } });
  await userEvent.click(create);
  await screen.findByRole("alert");
  await userEvent.click(create);
  await screen.findByRole("dialog", { name: "Authorized finance snapshot" });
  const posts = calls.filter((call) => call.method === "POST");
  expect(posts).toHaveLength(2);
  expect(posts[0].headers.get("Idempotency-Key")).toBe(posts[1].headers.get("Idempotency-Key"));
  const payload = { start_at: usage.start, end_at: usage.end, currency: "USD", timezone: "Asia/Shanghai" };
  expect(posts[0].body).toEqual(payload);
  expect(posts[0].url.pathname).toBe("/retail/organizations/org-one/finance/reports");
});
it("builds directory finance links only from one bounded scopes request and an effective grant", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailOrganizationsPanel />);
  expect(await screen.findByRole("link", { name: "Finance", exact: true })).toHaveAttribute(
    "href",
    "/ui/organization-finance?organization=org-one",
  );
  const scopes = calls.filter((call) => call.url.pathname === "/retail/organization-finance/organizations");
  expect(scopes).toHaveLength(1);
  expect(scopes[0].url.searchParams.get("ids")).toBe("org-one");
  expect(calls.some((call) => call.url.pathname.includes("/finance/access"))).toBe(false);
});
it("does not give an ungranted member a directory finance link", async () => {
  const { state } = serve();
  state.canView = false;
  renderWithProviders(<RetailOrganizationsPanel />);
  await screen.findByText(organization.name);
  expect(screen.queryByRole("link", { name: "Finance", exact: true })).not.toBeInTheDocument();
});
it.each(["Internal User", "Org Admin"])("does not expose grant administration to native role %s", async (role) => {
  const { calls } = serve();
  auth.userRole = role;
  grantForm();
  expect(calls).toHaveLength(0);
});
it("supports first view-only approval and shows that organization history is included", async () => {
  const { calls } = serve();
  auth.userRole = "Admin";
  grantForm();
  const select = await screen.findByRole("combobox", { name: "Finance permission" });
  expect(screen.getByText(/existing attributed usage history/)).toBeInTheDocument();
  fireEvent.change(select, { target: { value: "view" } });
  await userEvent.click(screen.getByRole("button", { name: "Approve finance access" }));
  await screen.findByText("Finance access saved.");
  const patch = calls.find((call) => call.method === "PATCH");
  expect(patch?.body).toEqual({ can_view: true, can_export: false, expected_version: 0 });
});
it("renews stale approval explicitly and reuses its key after an uncertain patch", async () => {
  const { state, calls } = serve();
  auth.userRole = "Admin";
  state.networkFailures = 1;
  state.grant = { ...grant, can_view: true, can_export: true, version: 7, state: "membership_changed" };
  grantForm();
  await screen.findByText("Membership changed — approval must be renewed");
  await userEvent.click(screen.getByRole("button", { name: "Approve finance access" }));
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: "Approve finance access" }));
  await screen.findByText("Finance access saved.");
  const patches = calls.filter((call) => call.method === "PATCH");
  expect(patches[0].body).toEqual({ can_view: true, can_export: true, expected_version: 7 });
  expect(patches[0].headers.get("Idempotency-Key")).toBe(patches[1].headers.get("Idempotency-Key"));
});
it("forces reload after a grant version conflict", async () => {
  const { state } = serve();
  auth.userRole = "Admin";
  state.writeStatus = 409;
  grantForm();
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "view" } });
  await userEvent.click(screen.getByRole("button", { name: "Approve finance access" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Reload before saving again");
  expect(screen.getByRole("button", { name: "Approve finance access" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Reload finance access" }));
  await waitFor(() => expect(screen.getByRole("combobox")).toBeEnabled());
});
it("reports eligibility validation for grant 422 without leaking raw details or window guidance", async () => {
  const { state } = serve();
  auth.userRole = "Admin";
  state.writeStatus = 422;
  grantForm();
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "view" } });
  await userEvent.click(screen.getByRole("button", { name: "Approve finance access" }));
  const error = await screen.findByRole("alert");
  expect(error).toHaveTextContent("member is no longer eligible");
  expect(error).not.toHaveTextContent("31 days");
  expect(error).not.toHaveTextContent("private-source-marker");
});

it("allows clearing an old grant for a demoted member while preventing a new approval", async () => {
  const { state, calls } = serve();
  auth.userRole = "Admin";
  state.grant = { ...grant, can_view: true, version: 2, state: "membership_inactive" };
  renderWithProviders(
    <FinanceGrantDialog
      organization={organization}
      member={{ ...member, role: "member" }}
      onClose={vi.fn()}
      onSaved={vi.fn()}
    />,
  );
  const select = await screen.findByRole("combobox");
  expect(screen.getByRole("option", { name: "View and export" })).toBeDisabled();
  fireEvent.change(select, { target: { value: "none" } });
  await userEvent.click(screen.getByRole("button", { name: "Clear finance access" }));
  await screen.findByText("Finance access saved.");
  expect(calls.find((call) => call.method === "PATCH")?.body).toEqual({
    can_view: false,
    can_export: false,
    expected_version: 2,
  });
});
it("keeps the grant management entry available to global admin for a demoted member with a historical grant", async () => {
  const { state } = serve();
  state.member = { ...member, role: "member" };
  auth.userRole = "Admin";
  renderWithProviders(
    <OrganizationMembers
      organization={organization}
      accessToken="session-one"
      actorId="reader-one"
      onClose={vi.fn()}
      onOrganizationChange={vi.fn()}
    />,
  );
  expect(await screen.findByRole("button", { name: "Finance access for customer-admin" })).toBeInTheDocument();
});
