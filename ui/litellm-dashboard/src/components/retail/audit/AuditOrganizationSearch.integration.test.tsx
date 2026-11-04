import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import type { RetailOrganization } from "../organizations/types";
import RetailOperationAuditPanel from "./RetailOperationAuditPanel";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    accessToken: "test-session",
    userId: "organization-admin",
    userRole: "Internal User",
    isViewOnly: false,
    isAuthorized: true,
    isLoading: false,
  }),
}));

const organization: RetailOrganization = {
  id: "organization-126",
  name: "North office",
  enabled: true,
  version: 2,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
  member_role: "admin",
  can_edit: false,
  can_associate_members: false,
  can_manage_members: true,
  can_view_events: true,
};
function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}
const event = {
  id: "own-event",
  organization_id: organization.id,
  actor_id: "organization-admin",
  action: "organization.updated",
  resource_type: "organization",
  resource_id: organization.id,
  result: "succeeded",
  version_before: 1,
  version_after: 2,
  created_at: organization.updated_at,
  changes: { name: organization.name },
};

function serve() {
  const calls: URL[] = [];
  const state = { revoked: false };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const url = new URL(input);
      calls.push(url);
      if (url.pathname === "/retail/config") return json({ operation_audit_enabled: true });
      if (url.pathname === "/retail/organizations") {
        if (state.revoked) return json({ detail: "Scope access revoked" }, 403);
        const matching = url.searchParams.get("offset") === "25" || url.searchParams.get("q") === "North";
        const page = {
          items: matching ? [organization] : [{ ...organization, id: "first-page", name: "West office" }],
          total: 126,
          limit: 25,
          offset: Number(url.searchParams.get("offset")),
        };
        return json(page);
      }
      if (url.pathname === `/retail/organizations/${organization.id}/operation-events`) {
        const page = { items: [event], total: 1, limit: 25, offset: 0 };
        return json(page);
      }
      return json({ detail: "Unexpected endpoint" }, 500);
    }),
  );
  return { calls, state };
}

beforeEach(() => testQueryClient.clear());
afterEach(() => {
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

it("selects a second-page authorized organization with a bounded server page", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailOperationAuditPanel />);
  const user = userEvent.setup();
  const search = await screen.findByRole("region", { name: "Find your audit organization" });
  const next = await within(search).findByRole("button", { name: "Next page" });
  await waitFor(() => expect(next).toBeEnabled());
  await user.click(next);
  expect(await screen.findByRole("option", { name: "North office" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Organization"), { target: { value: organization.id } });
  await user.click(screen.getByRole("button", { name: "Apply filters" }));
  expect(await screen.findByRole("button", { name: "View event own-event" })).toBeInTheDocument();
  expect(
    calls
      .filter((url) => url.pathname === "/retail/organizations")
      .map((url) => ({ limit: url.searchParams.get("limit"), offset: url.searchParams.get("offset") })),
  ).toEqual([
    { limit: "25", offset: "0" },
    { limit: "25", offset: "25" },
  ]);
  expect(calls.some((url) => url.pathname.startsWith("/retail/admin/"))).toBe(false);
});

it("searches authorized organizations on the server and resets pagination without probing a URL ID", async () => {
  const { calls } = serve();
  const onUrlUpdate = vi.fn();
  renderWithProviders(<RetailOperationAuditPanel />, {
    searchParams: { scope_page: "3", organization: "foreign-organization", retained: "yes" },
    onUrlUpdate,
  });
  const input = await screen.findByLabelText("Search your organizations");
  fireEvent.change(input, { target: { value: "North" } });
  expect(await screen.findByRole("option", { name: "North office" })).toBeInTheDocument();
  const searchCalls = calls.filter((url) => url.pathname === "/retail/organizations");
  expect(searchCalls.at(-1)?.searchParams.get("q")).toBe("North");
  expect(searchCalls.at(-1)?.searchParams.get("offset")).toBe("0");
  expect(calls.some((url) => url.pathname.includes("foreign-organization"))).toBe(false);
  expect(onUrlUpdate.mock.lastCall?.[0].searchParams.get("retained")).toBe("yes");
});

it("removes second-page options and event details when organization access is revoked", async () => {
  const { state } = serve();
  renderWithProviders(<RetailOperationAuditPanel />, {
    searchParams: { scope_page: "2", organization: organization.id },
  });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "View event own-event" }));
  expect(await screen.findByRole("dialog", { name: "Operation event" })).toBeInTheDocument();
  state.revoked = true;
  await testQueryClient.invalidateQueries({
    queryKey: ["retail", "organizations", "organization-admin", "audit-scope"],
  });
  expect(await screen.findByText("Unable to load your organizations")).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "North office" })).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "Operation event" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "View event own-event" })).not.toBeInTheDocument();
});
