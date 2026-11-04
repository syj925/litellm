import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../../tests/test-utils";
import DelegatedFinancePanel from "./DelegatedFinancePanel";

vi.unmock("@/app/(dashboard)/hooks/useAuthorized");
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
  getUiConfig: async () => ({
    admin_ui_disabled: false,
    server_root_path: "/",
    proxy_base_url: null,
    auto_redirect_to_sso: false,
    sso_configured: false,
  }),
}));

const revision = "59b1b66f-7eea-454e-b868-ad6fb10592fc";
const window = { start: "2026-11-01T00:00:00Z", end: "2026-11-02T00:00:00Z" };
function token() {
  const payload = {
    user_id: "native-customer",
    key: "synthetic-customer-session",
    user_role: "customer",
    exp: Math.floor(Date.now() / 1000) + 3600,
  };
  const encoded = btoa(JSON.stringify(payload)).replaceAll("=", "").replaceAll("+", "-").replaceAll("/", "_");
  return `eyJhbGciOiJub25lIn0.${encoded}.signature`;
}
function serve(granted: boolean) {
  const calls: { path: string; headers: Headers }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const path = new URL(input).pathname;
      calls.push({ path, headers: new Headers(options?.headers) });
      let body: unknown;
      if (path === "/retail/config")
        body = { org_finance_enabled: true, org_finance_delegation_enabled: true, report_export_enabled: true };
      else if (path === "/retail/organization-finance/organizations")
        body = {
          items: granted
            ? [
                {
                  id: "customer-org",
                  name: "Customer organization",
                  can_view: true,
                  can_export: false,
                  access_revision: revision,
                },
              ]
            : [],
          total: granted ? 1 : 0,
          limit: 25,
          offset: 0,
        };
      else if (path.endsWith("/finance/access"))
        body = {
          organization_id: "customer-org",
          organization_name: "Customer organization",
          can_view: granted,
          can_export: false,
          access_revision: granted ? revision : null,
        };
      else if (path.endsWith("/finance/consumption"))
        body = {
          organization_id: "customer-org",
          organization_name: "Customer organization",
          access_revision: revision,
          axis: "admission",
          snapshot_at: "2026-11-04T00:00:00Z",
          ...window,
          partial: false,
          live: true,
          max_requests: 10000,
          total: 0,
          limit: 25,
          offset: 0,
          summary: {
            accepted_requests: 0,
            settled_requests: 0,
            released_requests: 0,
            pending_requests: 0,
            unknown_requests: 0,
            conflict_requests: 0,
            settled_usd: "0.000000009",
            settled_nanos: "9",
          },
          items: [],
        };
      else throw new Error(`Unexpected customer request ${path}`);
      return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}
beforeEach(() => {
  testQueryClient.clear();
  document.cookie = `token=${token()}; path=/;`;
});
afterEach(() => {
  document.cookie = "token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
  vi.unstubAllGlobals();
  testQueryClient.clear();
});

it("takes a real native customer JWT through useAuthorized into only its explicitly granted finance projection", async () => {
  const calls = serve(true);
  renderWithProviders(<DelegatedFinancePanel />, { searchParams: { organization: "customer-org", ...window } });
  expect(await screen.findByRole("region", { name: "Authorized organization usage" })).toBeInTheDocument();
  expect(screen.getByText("$0.000000009")).toBeInTheDocument();
  const request = calls.find((call) => call.path.endsWith("/finance/consumption"));
  expect(request?.headers.get("Authorization")).toBe("Bearer synthetic-customer-session");
  expect(calls.some((call) => call.path.includes("/admin/") || call.path.includes("/reports"))).toBe(false);
});
it("does not expose organization consumption to an ungranted native customer", async () => {
  const calls = serve(false);
  renderWithProviders(<DelegatedFinancePanel />, { searchParams: { organization: "customer-org", ...window } });
  await screen.findByText("Your account has no finance view grant for this organization.");
  await waitFor(() => expect(screen.queryByRole("table")).not.toBeInTheDocument());
  expect(calls.some((call) => /consumption|reports|\/admin\//.test(call.path))).toBe(false);
});
