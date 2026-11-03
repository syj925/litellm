import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../tests/test-utils";
import KeysPanel from "./KeysPanel";

const auth = vi.hoisted(() => ({ isViewOnly: false }));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ ...auth, accessToken: "test-session", userId: "key-owner", userRole: "Internal User" }),
}));

const key = {
  token: "old-key-hash",
  token_id: "old-key-id",
  key_name: "sk-...example",
  key_alias: "Application key",
  spend: 2,
  max_budget: null,
  expires: null,
  created_at: "2026-01-01T00:00:00Z",
};
type Call = { path: string; method: string; body: unknown; headers: Headers };
function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}
function serve(rotationEnabled: boolean, configStatus = 200) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options?: RequestInit) => {
      const path = new URL(input, "http://localhost").pathname;
      const request = {
        path,
        method: options?.method ?? "GET",
        body: options?.body ? (JSON.parse(String(options.body)) as unknown) : null,
        headers: new Headers(options?.headers),
      };
      calls.push(request);
      if (path === "/retail/config") return json({ key_rotation_enabled: rotationEnabled }, configStatus);
      if (path === "/key/list") return json({ keys: [key] });
      if (path === "/retail/keys/rotate" || path.endsWith("/regenerate")) {
        const rotated = {
          key: "sk-new-test-secret",
          token_id: "new-key-id",
          key_name: "sk-...new",
          rotated_at: "2026-01-02T00:00:00Z",
        };
        return json(rotated);
      }
      return json({ detail: "Unexpected request" }, 500);
    }),
  );
  return calls;
}

beforeEach(() => {
  auth.isViewOnly = false;
  testQueryClient.clear();
});
afterEach(() => {
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

it.each([false, true])("uses retail rotation for premiumUser=%s without changing key settings", async (premiumUser) => {
  const calls = serve(true);
  renderWithProviders(<KeysPanel accessToken="test-session" userId="key-owner" premiumUser={premiumUser} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Rotate" }));
  const dialog = await screen.findByRole("dialog", { name: "Rotate API key" });
  expect(within(dialog).getByRole("button", { name: "Rotate key" })).toBeDisabled();
  expect(within(dialog).queryByText("Grace Period")).not.toBeInTheDocument();
  await user.click(within(dialog).getByRole("checkbox"));
  await user.click(within(dialog).getByRole("button", { name: "Rotate key" }));
  expect(await screen.findByRole("textbox", { name: "New API key" })).toHaveValue("sk-new-test-secret");
  const writes = calls.filter((call) => call.method === "POST");
  expect(writes.map((call) => ({ path: call.path, body: call.body }))).toEqual([
    { path: "/retail/keys/rotate", body: { key: "old-key-id" } },
  ]);
  expect(writes[0].headers.get("Authorization")).toBe("Bearer test-session");
  expect(JSON.stringify(testQueryClient.getQueryData(["chat-user-keys", "key-owner"]))).not.toContain(
    "sk-new-test-secret",
  );
  await user.click(within(dialog).getByRole("button", { name: "Done" }));
  expect(screen.queryByRole("textbox", { name: "New API key" })).not.toBeInTheDocument();
});

it("retains the native licensed rotation path only while retail rotation is disabled", async () => {
  const calls = serve(false);
  renderWithProviders(<KeysPanel accessToken="test-session" userId="key-owner" premiumUser />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Rotate" }));
  const dialog = await screen.findByRole("dialog", { name: "Rotate Key" });
  expect(within(dialog).getByText("Grace Period")).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Rotate" }));
  expect(await screen.findByText("sk-new-test-secret")).toBeInTheDocument();
  expect(calls.filter((call) => call.method === "POST").map((call) => call.path)).toEqual([
    "/key/old-key-hash/regenerate",
  ]);
});

it("keeps rotation hidden for viewers even when retail and native rotation are available", async () => {
  auth.isViewOnly = true;
  const calls = serve(true);
  renderWithProviders(<KeysPanel accessToken="test-session" userId="key-owner" premiumUser />);
  expect(await screen.findByText("Application key")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Rotate" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.method === "POST")).toBe(false);
});

it("does not fall back to native regeneration when retail settings cannot be verified", async () => {
  const calls = serve(true, 503);
  renderWithProviders(<KeysPanel accessToken="test-session" userId="key-owner" premiumUser />);
  expect(await screen.findByText("Key rotation is unavailable")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Rotate" })).not.toBeInTheDocument();
  expect(calls.some((call) => call.path.includes("regenerate"))).toBe(false);
});

it("removes cached key rows and rotation controls after a key-list refresh is forbidden", async () => {
  serve(true);
  renderWithProviders(<KeysPanel accessToken="test-session" userId="key-owner" premiumUser={false} />);
  expect(await screen.findByRole("button", { name: "Rotate" })).toBeInTheDocument();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json({ detail: "Access revoked" }, 403)),
  );
  await testQueryClient.invalidateQueries({ queryKey: ["chat-user-keys"] });
  expect(await screen.findByText("Unable to load API keys")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByText("Application key")).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Rotate" })).not.toBeInTheDocument();
});
