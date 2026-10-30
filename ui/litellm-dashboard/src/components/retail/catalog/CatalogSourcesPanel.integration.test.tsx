import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../../tests/test-utils";
import CatalogSourcesPanel from "./CatalogSourcesPanel";

const session = vi.hoisted(() => ({ userRole: "Admin", isViewOnly: false }));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ accessToken: "fixture-token", ...session }),
}));
vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));

const status = {
  sources: [
    {
      source: "models_dev",
      label: "models.dev",
      url: "https://models.dev",
      last_attempt_at: "2026-01-01T00:00:00Z",
      last_success_at: "2026-01-01T00:00:00Z",
      record_count: 4,
      last_error: null,
      stale: false,
    },
    {
      source: "openrouter",
      label: "OpenRouter",
      url: "https://openrouter.ai",
      last_attempt_at: "2026-01-02T00:00:00Z",
      last_success_at: "2026-01-01T00:00:00Z",
      record_count: 3,
      last_error: "The source could not be reached.",
      stale: true,
    },
  ],
  syncing: false,
  refresh_interval_seconds: 21600,
};

describe("catalog source administration", () => {
  beforeEach(() => {
    session.userRole = "Admin";
    session.isViewOnly = false;
  });
  afterEach(() => vi.unstubAllGlobals());

  it("shows independent source health and retains the last successful record count after a source failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(status)));
    renderWithProviders(<CatalogSourcesPanel />);
    const source = await screen.findByRole("region", { name: "OpenRouter sync status" });
    expect(within(source).getByText("3")).toBeInTheDocument();
    expect(within(source).getByText("Sync issue")).toBeInTheDocument();
    expect(within(source).getByText(/last successful snapshot remains available/)).toBeInTheDocument();
    expect(screen.getByText(/Prices and customer discounts use site configuration/)).toBeInTheDocument();
  });

  it("syncs through the admin endpoint and displays the resulting snapshot status", async () => {
    const user = userEvent.setup();
    const after = {
      ...status,
      sources: status.sources.map((source) => ({
        ...source,
        record_count: source.record_count + 1,
        last_error: null,
        stale: false,
      })),
    };
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(Response.json(status))
      .mockImplementation(() => Promise.resolve(Response.json(after)));
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(<CatalogSourcesPanel />);
    await screen.findByRole("region", { name: "models.dev sync status" });
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByText(/Sync completed/)).toBeInTheDocument();
    expect(fetcher).toHaveBeenCalledWith(
      "/retail/admin/catalog-sources/sync",
      expect.objectContaining({ method: "POST" }),
    );
    expect(within(screen.getByRole("region", { name: "models.dev sync status" })).getByText("5")).toBeInTheDocument();
  });

  it("explains concurrent synchronization without displaying an arbitrary backend error", async () => {
    const user = userEvent.setup();
    const fetcher = vi
      .fn()
      .mockImplementation((_url: string, options: RequestInit) =>
        Promise.resolve(
          options.method === "POST"
            ? Response.json({ detail: "opaque backend message" }, { status: 409 })
            : Response.json(status),
        ),
      );
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(<CatalogSourcesPanel />);
    await screen.findByRole("region", { name: "models.dev sync status" });
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A catalog sync is already running");
    expect(screen.queryByText("opaque backend message")).not.toBeInTheDocument();
  });

  it("does not report completion when a successful HTTP response says synchronization is already running", async () => {
    const user = userEvent.setup();
    const fetcher = vi
      .fn()
      .mockImplementation((_url: string, options: RequestInit) =>
        Promise.resolve(Response.json(options.method === "POST" ? { ...status, syncing: true } : status)),
      );
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(<CatalogSourcesPanel />);
    await screen.findByRole("region", { name: "models.dev sync status" });
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByText("A catalog sync is already running. Reload status shortly.")).toBeInTheDocument();
    expect(screen.queryByText(/Sync completed/)).not.toBeInTheDocument();
  });

  it.each([
    { userRole: "Internal User", isViewOnly: false },
    { userRole: "Admin", isViewOnly: true },
    { userRole: "org_admin", isViewOnly: false },
  ])("does not request admin source data for $userRole viewOnly=$isViewOnly", async (auth) => {
    Object.assign(session, auth);
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(<CatalogSourcesPanel />);
    expect(screen.getByText("Proxy administrator access is required.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync now" })).not.toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled();
  });
});
