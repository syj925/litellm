import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppRouterContext, type AppRouterInstance } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import useAuthorized from "../hooks/useAuthorized";
import UsersPage from "./page";

const mutations = vi.hoisted(() => ({
  userDeleteCall: vi.fn(),
  userUpdateUserCall: vi.fn(),
  invitationCreateCall: vi.fn(),
  teamMemberAddCall: vi.fn(),
  teamMemberDeleteCall: vi.fn(),
}));
vi.unmock("@/app/(dashboard)/hooks/useAuthorized");
vi.mock("@/components/networking", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/networking")>()),
  ...mutations,
  getProxyBaseUrl: () => "https://gateway.example",
  getUiConfig: async () => ({ admin_ui_disabled: false, server_root_path: "/", proxy_base_url: null }),
  getProxyUISettings: async () => ({}),
  userListCall: async () => ({ users: [account], total: 1, page: 1, page_size: 25, total_pages: 1 }),
  userGetInfoV2: async () => ({ ...account, teams: ["team-one"], metadata: {} }),
  getPossibleUserRoles: async () => ({ internal_user: { ui_label: "Internal User" } }),
  modelAvailableCall: async () => ({ data: [] }),
  teamListCall: async () => [{ team_id: "team-one", team_alias: "Example team" }],
  teamInfoCall: async () => ({ team_info: { team_id: "team-one", team_alias: "Example team" } }),
  fetchMCPServers: async () => [],
  fetchMCPToolsets: async () => [],
  fetchMCPAccessGroups: async () => [],
}));

const account = {
  user_id: "account-one",
  user_email: "account@example.com",
  user_alias: "Example account",
  user_role: "internal_user",
  spend: 1,
  max_budget: 100,
  models: [],
  key_count: 0,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
  sso_user_id: null,
  budget_duration: "30d",
};
function session(role: "proxy_admin" | "proxy_admin_viewer") {
  const payload = {
    user_id: "admin-session",
    key: "synthetic-session",
    user_role: role,
    exp: Math.floor(Date.now() / 1000) + 3600,
  };
  const encoded = btoa(JSON.stringify(payload)).replaceAll("=", "").replaceAll("+", "-").replaceAll("/", "_");
  document.cookie = `token=eyJhbGciOiJub25lIn0.${encoded}.signature; path=/;`;
}
function SessionRole() {
  const { userRole, isViewOnly } = useAuthorized();
  return (
    <output aria-label="Session permission">
      {userRole}:{String(isViewOnly)}
    </output>
  );
}
const router: AppRouterInstance = {
  back: vi.fn(),
  forward: vi.fn(),
  refresh: vi.fn(),
  hmrRefresh: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
};
function Page() {
  return (
    <AppRouterContext.Provider value={router}>
      <SessionRole />
      <UsersPage />
    </AppRouterContext.Provider>
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  testQueryClient.clear();
  session("proxy_admin_viewer");
});
afterEach(() => {
  document.cookie = "token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
  testQueryClient.clear();
});

it("keeps a real viewer JWT normalized to Admin read-only throughout the Users list", async () => {
  renderWithProviders(<Page />);
  expect(await screen.findByText("account@example.com")).toBeInTheDocument();
  expect(screen.getByLabelText("Session permission")).toHaveTextContent("Admin:true");
  expect(screen.queryByRole("button", { name: /create.*user|select users|bulk edit/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: /default user settings/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Open user actions" }));
  expect(await screen.findByRole("menuitem", { name: "Copy user ID" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: /edit user|reset password|delete user/i })).not.toBeInTheDocument();
  for (const mutation of Object.values(mutations)) expect(mutation).not.toHaveBeenCalled();
});

it("preserves viewer detail reads without account, budget, password, or team mutation controls", async () => {
  renderWithProviders(<Page />, { searchParams: { user: account.user_id } });
  expect(await screen.findByRole("heading", { name: account.user_email })).toBeInTheDocument();
  expect(await screen.findByText("Example team")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /reset password|delete user|add team|remove/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: "Details" }));
  expect(await screen.findByRole("heading", { name: "User Settings" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /edit settings|save changes/i })).not.toBeInTheDocument();
  expect(screen.queryByPlaceholderText("Max spend ($)")).not.toBeInTheDocument();
  for (const mutation of Object.values(mutations)) expect(mutation).not.toHaveBeenCalled();
});

it("retains administrator write entries and removes an open cost editor when the session becomes view-only", async () => {
  session("proxy_admin");
  const rendered = renderWithProviders(<Page />, { searchParams: { user: account.user_id } });
  expect(await screen.findByRole("button", { name: "Reset Password" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: "Details" }));
  await userEvent.click(await screen.findByRole("button", { name: "Edit Settings" }));
  expect(await screen.findByRole("button", { name: "Save Changes" })).toBeInTheDocument();
  session("proxy_admin_viewer");
  rendered.rerender(<Page />);
  await waitFor(() => expect(screen.queryByRole("button", { name: "Save Changes" })).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: /reset password|delete user|edit settings/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  for (const mutation of Object.values(mutations)) expect(mutation).not.toHaveBeenCalled();
});
