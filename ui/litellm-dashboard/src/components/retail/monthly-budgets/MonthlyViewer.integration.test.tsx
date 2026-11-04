import { screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import AdminMonthlyBudgets from "./AdminMonthlyBudgets";

vi.unmock("@/app/(dashboard)/hooks/useAuthorized");
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
  getUiConfig: async () => ({ admin_ui_disabled: false, server_root_path: "/", proxy_base_url: null }),
}));
function session(role: "proxy_admin" | "proxy_admin_viewer") {
  const payload = {
    user_id: "native-actor",
    key: "synthetic-session",
    user_role: role,
    exp: Math.floor(Date.now() / 1000) + 3600,
  };
  const encoded = btoa(JSON.stringify(payload)).replaceAll("=", "").replaceAll("+", "-").replaceAll("/", "_");
  document.cookie = `token=eyJhbGciOiJub25lIn0.${encoded}.signature; path=/;`;
}
function LegacyHost() {
  const { userRole, isViewOnly } = useAuthorized();
  return (
    <>
      <output aria-label="Native session permission">
        {userRole}:{String(isViewOnly)}
      </output>
      <AdminMonthlyBudgets userId="account-one" onSavingChange={() => undefined} />
    </>
  );
}
beforeEach(() => {
  testQueryClient.clear();
  session("proxy_admin_viewer");
});
afterEach(() => {
  document.cookie = "token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
  vi.unstubAllGlobals();
  testQueryClient.clear();
});

it("blocks a real viewer JWT normalized to Admin even when the host passes no read-only prop", async () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  renderWithProviders(<LegacyHost />);
  expect(await screen.findByLabelText("Native session permission")).toHaveTextContent("Admin:true");
  expect(screen.queryByRole("button", { name: "New monthly plan" })).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalled();
});

it("hides mounted monthly management immediately when the real session becomes view-only", async () => {
  session("proxy_admin");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const body =
        new URL(input).pathname === "/retail/config"
          ? { model_budgets_enabled: true, monthly_budgets_enabled: true }
          : { items: [], total: 0, limit: 25, offset: 0 };
      return new Response(JSON.stringify(body));
    }),
  );
  const rendered = renderWithProviders(<LegacyHost />);
  expect(await screen.findByRole("button", { name: "New monthly plan" })).toBeInTheDocument();
  session("proxy_admin_viewer");
  rendered.rerender(<LegacyHost />);
  expect(screen.getByLabelText("Native session permission")).toHaveTextContent("Admin:true");
  expect(screen.queryByRole("button", { name: "New monthly plan" })).not.toBeInTheDocument();
  expect(screen.queryByText("No matching monthly plans")).not.toBeInTheDocument();
});
