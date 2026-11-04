import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import RetailBusinessLabelsPanel from "./RetailBusinessLabelsPanel";
import type { MetadataTarget } from "./types";

const auth = vi.hoisted(() => ({ userRole: "Admin", isViewOnly: false }));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "https://gateway.example",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    ...auth,
    userId: "label-editor",
    accessToken: "test-session",
    isAuthorized: true,
    isLoading: false,
  }),
}));

const target: MetadataTarget = {
  id: "23648472-a962-4cd4-ad96-ce9c093b44d1",
  kind: "key",
  name: "Application access",
  object_created_at: "2026-10-01T00:00:00Z",
  object_created_by: "native-object-owner",
  object_updated_at: null,
  object_updated_by: null,
  metadata: {
    tags: [],
    version: 0,
    source: null,
    created_at: null,
    created_by: null,
    updated_at: null,
    updated_by: null,
  },
};
type ApiCall = { url: URL; method: string; body: unknown; headers: Headers };
type ServerState = {
  target: MetadataTarget;
  enabled: boolean;
  listStatus: number;
  detailStatus: number;
  saveStatus: number;
  total: number;
  second?: MetadataTarget;
};
function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}
function serve() {
  const calls: ApiCall[] = [];
  const state: ServerState = { target, enabled: true, listStatus: 200, detailStatus: 200, saveStatus: 200, total: 1 };
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
      if (call.url.pathname === "/retail/config") return json({ object_metadata_enabled: state.enabled });
      if (call.url.pathname.endsWith("/targets")) {
        if (state.listStatus !== 200) return json({ detail: "Object authority unavailable" }, state.listStatus);
        const selected = call.url.searchParams.get("offset") === "25" ? state.second ?? state.target : state.target;
        const page = {
          items: [selected],
          total: state.total,
          limit: 25,
          offset: Number(call.url.searchParams.get("offset")),
          scope_note: "Only supported existing objects are available.",
        };
        return json(page);
      }
      if (call.method === "PATCH") {
        if (state.saveStatus !== 200)
          return json(
            { detail: state.saveStatus === 409 ? "Version conflict" : "Object access denied" },
            state.saveStatus,
          );
        const payload = call.body as { tags: string[]; expected_version: number };
        state.target = {
          ...state.target,
          metadata: {
            tags: [...payload.tags].sort(),
            version: payload.expected_version + 1,
            source: "management_api",
            created_at: "2026-11-04T00:00:00Z",
            created_by: "label-editor",
            updated_at: "2026-11-04T00:00:00Z",
            updated_by: "label-editor",
          },
        };
        return json(state.target);
      }
      const detail = state.second && call.url.pathname.endsWith(state.second.id) ? state.second : state.target;
      return state.detailStatus === 200 ? json(detail) : json({ detail: "Object access denied" }, state.detailStatus);
    }),
  );
  return { calls, state };
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

async function openEditor() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Edit labels for Application access" }));
  return await screen.findByRole("textbox", { name: "Business labels" });
}

it.each([
  { role: "Internal User", viewer: false },
  { role: "Org Admin", viewer: false },
  { role: "Admin", viewer: true },
  { role: "proxy_admin_viewer", viewer: false },
])("does not request administrator data for $role (viewer=$viewer)", async ({ role, viewer }) => {
  auth.userRole = role;
  auth.isViewOnly = viewer;
  const { calls } = serve();
  renderWithProviders(<RetailBusinessLabelsPanel />);
  expect(await screen.findByText("Gateway administrator access required")).toBeInTheDocument();
  expect(calls).toHaveLength(0);
});

it("shows the disabled feature state without falling back to native metadata", async () => {
  const { state, calls } = serve();
  state.enabled = false;
  renderWithProviders(<RetailBusinessLabelsPanel />);
  expect(await screen.findByText("Business labels is not enabled")).toBeInTheDocument();
  expect(calls.map((call) => call.url.pathname)).toEqual(["/retail/config"]);
});

it("saves labels by opaque reference with CAS and keeps label provenance separate from object provenance", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailBusinessLabelsPanel />);
  const input = await openEditor();
  expect(screen.getByText("native-object-owner")).toBeInTheDocument();
  expect(screen.getByText("Label record created by")).toBeInTheDocument();
  fireEvent.change(input, { target: { value: "support\nproduction" } });
  await userEvent.setup().click(screen.getByRole("button", { name: "Save labels" }));
  expect(await screen.findByText("Management API")).toBeInTheDocument();
  const save = calls.find((call) => call.method === "PATCH");
  expect(save?.url.pathname).toBe(`/retail/admin/object-metadata/key/${target.id}`);
  expect(save?.body).toEqual({ tags: ["support", "production"], expected_version: 0 });
  expect(save?.headers.get("Idempotency-Key")).toBeTruthy();
  expect(screen.getByText("native-object-owner")).toBeInTheDocument();
  expect(screen.getAllByText("label-editor")).toHaveLength(2);
});

it("requires an explicit reload after a version conflict before applying another change", async () => {
  const { calls, state } = serve();
  state.saveStatus = 409;
  renderWithProviders(<RetailBusinessLabelsPanel />);
  const input = await openEditor();
  fireEvent.change(input, { target: { value: "support" } });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Save labels" }));
  expect(await screen.findByText("Labels changed")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save labels" })).toBeDisabled();
  state.target = { ...target, metadata: { ...target.metadata, tags: ["reviewed"], version: 2 } };
  state.saveStatus = 200;
  await user.click(screen.getByRole("button", { name: "Reload label record" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Business labels" })).toHaveValue("reviewed"));
  fireEvent.change(screen.getByRole("textbox", { name: "Business labels" }), { target: { value: "" } });
  await user.click(screen.getByRole("button", { name: "Save labels" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PATCH")).toHaveLength(2));
  expect(calls.filter((call) => call.method === "PATCH").at(-1)?.body).toEqual({ tags: [], expected_version: 2 });
});

it("hides cached rows and provenance when the target authority is unavailable", async () => {
  const { state } = serve();
  renderWithProviders(<RetailBusinessLabelsPanel />);
  await openEditor();
  state.listStatus = 503;
  await userEvent.setup().click(screen.getByRole("button", { name: "Refresh objects" }));
  expect(await screen.findByText("Unable to load objects")).toBeInTheDocument();
  expect(screen.queryByText("Application access")).not.toBeInTheDocument();
  expect(screen.queryByText("native-object-owner")).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Business labels" })).not.toBeInTheDocument();
});

it("removes all cached object controls immediately when a save is denied", async () => {
  const { state } = serve();
  state.saveStatus = 403;
  renderWithProviders(<RetailBusinessLabelsPanel />);
  fireEvent.change(await openEditor(), { target: { value: "support" } });
  await userEvent.setup().click(screen.getByRole("button", { name: "Save labels" }));
  expect(await screen.findByText("Object access changed")).toBeInTheDocument();
  expect(screen.queryByText("Application access")).not.toBeInTheDocument();
  expect(screen.queryByText("native-object-owner")).not.toBeInTheDocument();
});

it("never probes a token hash supplied as a target URL parameter", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailBusinessLabelsPanel />, { searchParams: { target: "f".repeat(64) } });
  expect(await screen.findByText("Application access")).toBeInTheDocument();
  expect(calls.map((call) => call.url.pathname)).toEqual(["/retail/config", "/retail/admin/object-metadata/targets"]);
});

it("uses bounded target pagination and server search while clearing the previous selection", async () => {
  const { calls, state } = serve();
  state.total = 26;
  state.second = { ...target, id: "a9cf946b-8917-4755-a944-76f63030482a", name: "Second page application" };
  renderWithProviders(<RetailBusinessLabelsPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Next page" }));
  await user.click(await screen.findByRole("button", { name: "Edit labels for Second page application" }));
  expect(await screen.findByRole("textbox", { name: "Business labels" })).toBeInTheDocument();
  const targetCalls = calls.filter((call) => call.url.pathname.endsWith("/targets"));
  expect(targetCalls.map((call) => call.url.searchParams.get("offset"))).toEqual(["0", "25"]);
  expect(targetCalls.every((call) => call.url.searchParams.get("limit") === "25")).toBe(true);
  fireEvent.change(screen.getByRole("textbox", { name: "Search object names" }), { target: { value: "Support" } });
  await waitFor(() =>
    expect(
      calls
        .filter((call) => call.url.pathname.endsWith("/targets"))
        .at(-1)
        ?.url.searchParams.get("q"),
    ).toBe("Support"),
  );
  expect(
    calls
      .filter((call) => call.url.pathname.endsWith("/targets"))
      .at(-1)
      ?.url.searchParams.get("offset"),
  ).toBe("0");
  expect(screen.queryByRole("textbox", { name: "Business labels" })).not.toBeInTheDocument();
});

it("retries a failed save with the same idempotency key and unchanged label version", async () => {
  const { calls, state } = serve();
  state.saveStatus = 503;
  renderWithProviders(<RetailBusinessLabelsPanel />);
  fireEvent.change(await openEditor(), { target: { value: "support" } });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Save labels" }));
  expect(await screen.findByText("Unable to save labels")).toBeInTheDocument();
  state.saveStatus = 200;
  await user.click(screen.getByRole("button", { name: "Save labels" }));
  expect(await screen.findByText("Management API")).toBeInTheDocument();
  const writes = calls.filter((call) => call.method === "PATCH");
  expect(writes).toHaveLength(2);
  expect(writes[0].headers.get("Idempotency-Key")).toBeTruthy();
  expect(writes[1].headers.get("Idempotency-Key")).toBe(writes[0].headers.get("Idempotency-Key"));
  expect(writes[1].body).toEqual({ tags: ["support"], expected_version: 0 });
});

it("rejects identifier-like labels before sending a write", async () => {
  const { calls } = serve();
  renderWithProviders(<RetailBusinessLabelsPanel />);
  fireEvent.change(await openEditor(), { target: { value: "abcdef0123456789" } });
  await userEvent.setup().click(screen.getByRole("button", { name: "Save labels" }));
  expect(
    await screen.findByText("Use a business category instead of an identifier or credential."),
  ).toBeInTheDocument();
  expect(calls.some((call) => call.method === "PATCH")).toBe(false);
});

it("drops cached object controls when a fresh record read is forbidden", async () => {
  const { state } = serve();
  renderWithProviders(<RetailBusinessLabelsPanel />);
  await openEditor();
  state.detailStatus = 403;
  await userEvent.setup().click(screen.getByRole("button", { name: "Reload label record" }));
  expect(await screen.findByText("Object access changed")).toBeInTheDocument();
  expect(screen.queryByText("Application access")).not.toBeInTheDocument();
  expect(screen.queryByText("native-object-owner")).not.toBeInTheDocument();
});

it("stops at the supported browse limit and asks the administrator to narrow large searches", async () => {
  const { state, calls } = serve();
  state.total = 10_026;
  renderWithProviders(<RetailBusinessLabelsPanel />, { searchParams: { page: "401" } });
  expect(await screen.findByText(/Narrow your search to reach objects beyond the first 10,025/)).toHaveTextContent(
    "10,026 matching objects",
  );
  expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Previous page" })).toBeEnabled();
  const pageRequest = calls.find((call) => call.url.pathname.endsWith("/targets"));
  expect(pageRequest?.url.searchParams.get("offset")).toBe("10000");
});
