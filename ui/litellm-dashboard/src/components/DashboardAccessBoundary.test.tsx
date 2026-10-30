import { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen } from "../../tests/test-utils";
import DashboardAccessBoundary from "./DashboardAccessBoundary";

const state = vi.hoisted(() => ({ pathname: "/ui/models-and-endpoints/", role: "Internal User", mounts: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => state.pathname }));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    isLoading: false,
    isAuthorized: true,
    userRole: state.role,
    isViewOnly: false,
    userId: "customer",
  }),
}));
vi.mock("@/app/(dashboard)/hooks/useIsOrgAdmin", () => ({ default: () => false }));
vi.mock("@/app/(dashboard)/hooks/teams/useTeams", () => ({ useTeams: () => ({ data: [], isLoading: false }) }));
vi.mock("@/app/(dashboard)/hooks/uiSettings/useUISettings", () => ({
  useUISettings: () => ({ data: { values: {} }, isPending: false }),
}));

function PageContent() {
  useEffect(() => state.mounts(), []);
  return <h1>Protected page data</h1>;
}

beforeEach(() => {
  state.pathname = "/ui/models-and-endpoints/";
  state.role = "Internal User";
  state.mounts.mockClear();
});

describe("dashboard page boundary", () => {
  it("rejects a direct management URL before the protected page mounts", () => {
    renderWithProviders(
      <DashboardAccessBoundary>
        <PageContent />
      </DashboardAccessBoundary>,
    );
    expect(screen.getByText("Access required")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Protected page data" })).not.toBeInTheDocument();
    expect(state.mounts).not.toHaveBeenCalled();
  });
  it("permits a customer's wallet without management access", () => {
    state.pathname = "/ui/wallet/";
    renderWithProviders(
      <DashboardAccessBoundary>
        <PageContent />
      </DashboardAccessBoundary>,
    );
    expect(screen.getByRole("heading", { name: "Protected page data" })).toBeInTheDocument();
    expect(state.mounts).toHaveBeenCalledOnce();
  });
  it("retains the management page for proxy administrators", () => {
    state.role = "Admin";
    renderWithProviders(
      <DashboardAccessBoundary>
        <PageContent />
      </DashboardAccessBoundary>,
    );
    expect(screen.getByRole("heading", { name: "Protected page data" })).toBeInTheDocument();
  });
  it("rejects a direct retail administration URL for a proxy viewer before page effects run", () => {
    state.role = "proxy_admin_viewer";
    state.pathname = "/ui/wallet-admin/";
    renderWithProviders(
      <DashboardAccessBoundary>
        <PageContent />
      </DashboardAccessBoundary>,
    );
    expect(screen.getByText("Access required")).toBeInTheDocument();
    expect(state.mounts).not.toHaveBeenCalled();
  });
});
