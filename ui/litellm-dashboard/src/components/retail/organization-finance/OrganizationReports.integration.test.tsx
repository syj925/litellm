import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import RetailReportsPanel from "../reports/RetailReportsPanel";
import OrganizationReportTasks from "./OrganizationReportTasks";
import RetailOrganizationConsumptionPanel from "./RetailOrganizationConsumptionPanel";
import type { CreateOrganizationReport, OrganizationConsumption, OrganizationReportJob } from "./types";

const auth = vi.hoisted(() => ({
  userRole: "Admin",
  isViewOnly: false,
  isAuthorized: true,
  accessToken: "session-one",
}));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ ...auth, userId: "fixture-admin", isLoading: false }),
}));

const summary = {
  accepted_requests: 6,
  settled_requests: 2,
  released_requests: 1,
  pending_requests: 1,
  unknown_requests: 1,
  conflict_requests: 1,
  settled_usd: "0.000000001",
  settled_nanos: "1",
};
const preview: OrganizationConsumption = {
  organization_id: "org-one",
  organization_name: "Example organization",
  organization_enabled: true,
  axis: "admission",
  snapshot_at: "2026-11-04T00:00:00Z",
  start: "2026-11-01T00:00:00Z",
  end: "2026-11-02T00:00:00Z",
  partial: false,
  live: true,
  max_requests: 10000,
  total: 6,
  limit: 25,
  offset: 0,
  summary,
  items: [],
};
const job: OrganizationReportJob = {
  id: "snapshot-one",
  scope: "organization",
  axis: "admission",
  organization_id: "org-one",
  organization_name: "Organization name at capture",
  state: "ready",
  start_at: preview.start,
  end_at: preview.end,
  snapshot_at: "2026-11-04T00:01:00Z",
  created_at: "2026-11-04T00:01:00Z",
  expires_at: "2026-11-06T00:01:00Z",
  partial: true,
  total_rows: 6,
  summary,
  artifact_bytes: 123,
  error_code: null,
};
type Call = {
  url: URL;
  method: string;
  body: unknown;
  headers: Headers;
  cache: RequestCache | undefined;
  credentials: RequestCredentials | undefined;
};
type Server = {
  finance: boolean;
  exports: boolean;
  jobs: OrganizationReportJob[];
  total: number;
  readStatus: number;
  createStatus: number;
  downloadStatus: number;
  networkFailures: number;
  previewEnabled: boolean;
};
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}
function page<T>(items: T[], total = items.length, offset = 0) {
  return { items, total, limit: 25, offset };
}
function createJob(call: Call, state: Server, organizationId: string) {
  if (state.networkFailures > 0) {
    state.networkFailures -= 1;
    throw new TypeError("Failed to fetch");
  }
  if (state.createStatus !== 202) return json({ detail: "private-storage-marker" }, state.createStatus);
  const body = call.body as CreateOrganizationReport;
  const created = {
    ...job,
    id: `created-${state.jobs.length}`,
    organization_id: organizationId,
    start_at: body.start_at,
    end_at: body.end_at,
    state: "queued" as const,
  };
  state.jobs.unshift(created);
  state.total += 1;
  return json(created, 202);
}
function serveExports(call: Call, state: Server) {
  const parts = call.url.pathname.split("/");
  const organizationId = parts[4];
  if (call.method === "POST") return createJob(call, state, organizationId);
  if (parts.at(-1) === "download") {
    if (state.downloadStatus !== 200) return json({ detail: "private-storage-marker" }, state.downloadStatus);
    return new Response("request_id,state\nrequest-one,settled\n", { headers: { "Content-Type": "text/csv" } });
  }
  if (state.readStatus !== 200) return json({ detail: "private-storage-marker" }, state.readStatus);
  if (parts.at(-1) === "reports")
    return json(
      page(
        state.jobs.filter((item) => item.organization_id === organizationId),
        state.total,
        Number(call.url.searchParams.get("offset")),
      ),
    );
  return json(state.jobs.find((item) => item.id === parts.at(-1)) ?? job);
}
function serve() {
  const calls: Call[] = [];
  const state: Server = {
    finance: true,
    exports: true,
    jobs: [structuredClone(job)],
    total: 1,
    readStatus: 200,
    createStatus: 202,
    downloadStatus: 200,
    networkFailures: 0,
    previewEnabled: true,
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
        credentials: options?.credentials,
      };
      calls.push(call);
      const path = call.url.pathname;
      if (path === "/retail/config")
        return json({ org_finance_enabled: state.finance, report_export_enabled: state.exports });
      if (path === "/retail/reports") return json(page([]));
      if (path === "/retail/admin/organizations")
        return json(
          page([
            { id: "org-one", name: "Example organization", enabled: true },
            { id: "org-two", name: "Second organization", enabled: true },
          ]),
        );
      if (path.endsWith("/consumption"))
        return json({ ...preview, organization_id: path.split("/")[4], organization_enabled: state.previewEnabled });
      if (path.includes("/reports")) return serveExports(call, state);
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  return { state, calls };
}
function posts(calls: Call[]) {
  return calls.filter((call) => call.method === "POST");
}
let clickedLinks: HTMLAnchorElement[] = [];
beforeEach(() => {
  const initialAuth = { userRole: "Admin", isViewOnly: false, isAuthorized: true, accessToken: "session-one" };
  Object.assign(auth, initialAuth);
  testQueryClient.clear();
  clickedLinks = [];
  const NativeURL = URL;
  vi.stubGlobal(
    "URL",
    class extends NativeURL {
      static createObjectURL = vi.fn(() => "blob:organization-csv");
      static revokeObjectURL = vi.fn();
    },
  );
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clickedLinks.push(this);
  });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  testQueryClient.clear();
});

it.each([
  { userRole: "Internal User", isViewOnly: false, isAuthorized: true },
  { userRole: "Org Admin", isViewOnly: false, isAuthorized: true },
  { userRole: "proxy_admin_viewer", isViewOnly: false, isAuthorized: true },
  { userRole: "Admin", isViewOnly: true, isAuthorized: true },
  { userRole: "Admin", isViewOnly: false, isAuthorized: false },
])("does not fetch fixed organization tasks for %o", (role) => {
  const { calls } = serve();
  Object.assign(auth, role);
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  expect(calls).toHaveLength(0);
  expect(screen.queryByRole("region", { name: "Fixed organization exports" })).not.toBeInTheDocument();
});

it.each([
  { finance: false, exports: true },
  { finance: true, exports: false },
  { finance: false, exports: false },
])("requires both flags for fixed tasks %o", async (flags) => {
  const { state, calls } = serve();
  Object.assign(state, flags);
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  await waitFor(() => expect(calls.some((call) => call.url.pathname === "/retail/config")).toBe(true));
  expect(calls.some((call) => call.url.pathname.includes("/reports"))).toBe(false);
  expect(screen.queryByRole("region", { name: "Fixed organization exports" })).not.toBeInTheDocument();
});

it("keeps the live preview available when fixed export is disabled", async () => {
  const { state, calls } = serve();
  state.exports = false;
  renderWithProviders(<RetailOrganizationConsumptionPanel />, {
    searchParams: { organization: "org-one", start: preview.start, end: preview.end },
  });
  expect(await screen.findByText("Finalized retail charges (USD)")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Fixed organization exports" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/reports"))).toBe(false);
});

it("creates only the displayed successful admission window and requires a deliberate new task after success", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailOrganizationConsumptionPanel />, {
    searchParams: { organization: "org-one", start: preview.start, end: preview.end },
  });
  const create = await screen.findByRole("button", { name: "Create fixed CSV for displayed window" });
  await waitFor(() => expect(create).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Accepted from (Beijing)"), { target: { value: "2026-10-25T00:00" } });
  await userEvent.click(create);
  expect(await screen.findByRole("dialog", { name: "Organization CSV snapshot" })).toBeInTheDocument();
  const expectedPayload = {
    start_at: preview.start,
    end_at: preview.end,
    currency: "USD",
    timezone: "Asia/Shanghai",
  };
  expect(posts(calls)[0].body).toEqual(expectedPayload);
  expect(posts(calls)[0].headers.get("Idempotency-Key")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.getByRole("button", { name: "CSV task created" })).toBeDisabled();
  expect(posts(calls)).toHaveLength(1);
});

it("reuses the same idempotency key after an uncertain create result", async () => {
  const { state, calls } = serve();
  state.networkFailures = 1;
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  const create = await screen.findByRole("button", { name: "Create fixed CSV for displayed window" });
  await waitFor(() => expect(create).toBeEnabled());
  await userEvent.click(create);
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be confirmed");
  await userEvent.click(create);
  await screen.findByRole("dialog", { name: "Organization CSV snapshot" });
  expect(posts(calls)).toHaveLength(2);
  expect(posts(calls)[0].headers.get("Idempotency-Key")).toBe(posts(calls)[1].headers.get("Idempotency-Key"));
});

it("requires an explicit new snapshot action before creating another task for the same displayed window", async () => {
  const { calls } = serve();
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  const create = await screen.findByRole("button", { name: "Create fixed CSV for displayed window" });
  await waitFor(() => expect(create).toBeEnabled());
  await userEvent.click(create);
  await screen.findByRole("dialog", { name: "Organization CSV snapshot" });
  await userEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.getByRole("button", { name: "CSV task created" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Start a new snapshot" }));
  const next = screen.getByRole("button", { name: "Create fixed CSV for displayed window" });
  await waitFor(() => expect(next).toBeEnabled());
  await userEvent.click(next);
  await screen.findByRole("dialog", { name: "Organization CSV snapshot" });
  expect(posts(calls)).toHaveLength(2);
  expect(posts(calls)[0].headers.get("Idempotency-Key")).not.toBe(posts(calls)[1].headers.get("Idempotency-Key"));
});

it("clears an uncertain create attempt when the organization scope changes", async () => {
  const { state, calls } = serve();
  state.networkFailures = 2;
  const view = renderWithProviders(
    <OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />,
  );
  const create = await screen.findByRole("button", { name: "Create fixed CSV for displayed window" });
  await waitFor(() => expect(create).toBeEnabled());
  await userEvent.click(create);
  await screen.findByRole("alert");
  view.rerender(
    <OrganizationReportTasks
      organizationId="org-two"
      preview={{ ...preview, organization_id: "org-two" }}
      previewLoading={false}
    />,
  );
  const second = await screen.findByRole("button", { name: "Create fixed CSV for displayed window" });
  await waitFor(() => expect(second).toBeEnabled());
  await userEvent.click(second);
  await screen.findByRole("alert");
  expect(posts(calls)).toHaveLength(2);
  expect(posts(calls)[1].url.pathname).toBe("/retail/admin/organizations/org-two/reports");
  expect(posts(calls)[0].headers.get("Idempotency-Key")).not.toBe(posts(calls)[1].headers.get("Idempotency-Key"));
});

it("resets task pagination on organization selection without changing preview pagination by a task-page click", async () => {
  const { state, calls } = serve();
  const onUrlUpdate = vi.fn();
  state.total = 30;
  renderWithProviders(<RetailOrganizationConsumptionPanel />, {
    searchParams: {
      organization: "org-one",
      start: preview.start,
      end: preview.end,
      export_page: "2",
      page: "2",
      unrelated: "keep",
    },
    onUrlUpdate,
  });
  const region = await screen.findByRole("region", { name: "Fixed organization exports" });
  await screen.findByRole("button", { name: "Download organization export snapshot-one" });
  await userEvent.click(within(region).getByRole("button", { name: "Previous page" }));
  await waitFor(() => expect(onUrlUpdate.mock.lastCall?.[0].queryString).not.toContain("export_page=2"));
  expect(onUrlUpdate.mock.lastCall?.[0].queryString).toContain("page=2");
  fireEvent.change(screen.getByRole("combobox", { name: "Organization" }), { target: { value: "org-two" } });
  await waitFor(() =>
    expect(calls.some((call) => call.url.pathname === "/retail/admin/organizations/org-two/reports")).toBe(true),
  );
  expect(onUrlUpdate.mock.lastCall?.[0].queryString).toContain("unrelated=keep");
  expect(onUrlUpdate.mock.lastCall?.[0].queryString).not.toContain("export_page=");
});

it.each(["queued", "processing", "failed", "revoked", "expired"] as const)(
  "does not allow downloading task state %s",
  async (stateName) => {
    const { state } = serve();
    state.jobs[0].state = stateName;
    state.jobs[0].error_code = "private-storage-marker";
    renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
    expect(await screen.findByRole("button", { name: "Download organization export snapshot-one" })).toBeDisabled();
    expect(screen.queryByText("private-storage-marker")).not.toBeInTheDocument();
  },
);

it("polls queued tasks until the server marks the fixed artifact ready", async () => {
  const { state, calls } = serve();
  state.jobs[0].state = "queued";
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  const download = await screen.findByRole("button", { name: "Download organization export snapshot-one" });
  expect(download).toBeDisabled();
  state.jobs[0].state = "ready";
  await waitFor(() => expect(download).toBeEnabled(), { timeout: 4000 });
  expect(calls.filter((call) => call.url.pathname.endsWith("/reports")).length).toBeGreaterThan(1);
});

it("downloads a ready organization artifact with current authentication and no-store Blob handling", async () => {
  const { calls } = serve();
  const view = renderWithProviders(
    <OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />,
  );
  await screen.findByRole("button", { name: "Download organization export snapshot-one" });
  auth.accessToken = "fresh-session";
  view.rerender(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  await userEvent.click(screen.getByRole("button", { name: "Download organization export snapshot-one" }));
  await waitFor(() => expect(clickedLinks).toHaveLength(1));
  const download = calls.find((call) => call.url.pathname.endsWith("/download"));
  expect(download?.headers.get("Authorization")).toBe("Bearer fresh-session");
  expect(download?.headers.get("Accept")).toBe("text/csv");
  expect(download?.cache).toBe("no-store");
  expect(download?.credentials).toBe("same-origin");
  expect(download?.url.search).toBe("");
  expect(clickedLinks[0].download).toBe("organization-report-snapshot-one.csv");
  expect(clickedLinks[0].href).toBe("blob:organization-csv");
});

it.each([401, 403])("hides all task cache when download authority returns %s", async (status) => {
  const { state } = serve();
  state.downloadStatus = status;
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  await userEvent.click(await screen.findByRole("button", { name: "Download organization export snapshot-one" }));
  await screen.findByText("Export access changed. Reload export tasks to verify your current access.");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Download organization export snapshot-one" })).not.toBeInTheDocument();
});

it("hides fixed tasks after a detail authority failure", async () => {
  const { state } = serve();
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  const details = await screen.findByRole("button", { name: "View organization export snapshot-one" });
  state.readStatus = 403;
  await userEvent.click(details);
  await screen.findByText("Export access changed. Reload export tasks to verify your current access.");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("hides fixed tasks after a create authority failure while preserving the live preview", async () => {
  const { state } = serve();
  state.createStatus = 403;
  renderWithProviders(<RetailOrganizationConsumptionPanel />, {
    searchParams: { organization: "org-one", start: preview.start, end: preview.end },
  });
  const create = await screen.findByRole("button", { name: "Create fixed CSV for displayed window" });
  await waitFor(() => expect(create).toBeEnabled());
  await userEvent.click(create);
  await screen.findByText("Export access changed. Reload export tasks to verify your current access.");
  expect(screen.getByText("Finalized retail charges (USD)")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Download organization export snapshot-one" })).not.toBeInTheDocument();
});

it("hides fixed tasks after a list authority refresh fails", async () => {
  const { state } = serve();
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  await screen.findByRole("button", { name: "Download organization export snapshot-one" });
  state.readStatus = 403;
  await userEvent.click(screen.getByRole("button", { name: "Reload export tasks" }));
  await screen.findByText("Export access changed. Reload export tasks to verify your current access.");
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("does not leave cached fixed tasks visible after the signed-in role loses writable admin access", async () => {
  const { calls } = serve();
  const view = renderWithProviders(
    <OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />,
  );
  await screen.findByRole("button", { name: "Download organization export snapshot-one" });
  const count = calls.length;
  auth.userRole = "Org Admin";
  view.rerender(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  expect(screen.queryByRole("region", { name: "Fixed organization exports" })).not.toBeInTheDocument();
  expect(calls).toHaveLength(count);
});

it.each([404, 410])("refreshes artifact status when download returns %s", async (status) => {
  const { state, calls } = serve();
  state.downloadStatus = status;
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  const download = await screen.findByRole("button", { name: "Download organization export snapshot-one" });
  state.jobs[0].state = "expired";
  await userEvent.click(download);
  await waitFor(() => expect(download).toBeDisabled());
  expect(calls.filter((call) => call.url.pathname.endsWith("/reports")).length).toBeGreaterThan(1);
  expect(screen.queryByText("private-storage-marker")).not.toBeInTheDocument();
});

it("shows the frozen name and unresolved outcomes from the task detail response", async () => {
  const { calls } = serve();
  renderWithProviders(<OrganizationReportTasks organizationId="org-one" preview={preview} previewLoading={false} />);
  await userEvent.click(await screen.findByRole("button", { name: "View organization export snapshot-one" }));
  const detail = await screen.findByRole("dialog", { name: "Organization CSV snapshot" });
  expect(await within(detail).findByText("Organization name at capture")).toBeInTheDocument();
  const outcomes = within(detail).getByRole("region", { name: "Frozen request outcomes" });
  expect(within(outcomes).getByText("Pending")).toBeInTheDocument();
  expect(within(outcomes).getByText("Unknown")).toBeInTheDocument();
  expect(within(outcomes).getByText("Conflict")).toBeInTheDocument();
  expect(within(outcomes).getByText("$0.000000001")).toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.endsWith("/reports/snapshot-one"))).toBe(true);
});

it("does not request or expose fixed task data for a known disabled organization", async () => {
  const { calls } = serve();
  renderWithProviders(
    <OrganizationReportTasks
      organizationId="org-one"
      preview={{ ...preview, organization_enabled: false }}
      previewLoading={false}
    />,
  );
  await screen.findByText("Load an enabled organization’s preview to create or view fixed exports.");
  expect(calls.some((call) => call.url.pathname.includes("/reports"))).toBe(false);
});

it("keeps personal account reports separate from organization CSV task routes", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailReportsPanel />);
  expect(await screen.findByRole("heading", { name: "Create a wallet report" })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Fixed organization exports" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.url.pathname.includes("/admin/organizations/"))).toBe(false);
});
