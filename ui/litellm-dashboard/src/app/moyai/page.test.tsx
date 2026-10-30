import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen } from "../../../tests/test-utils";
import MoyaiPage from "./page";

const state = vi.hoisted(() => ({
  role: "Internal User",
  viewOnly: false,
  pluginProvider: vi.fn(),
  quickConnect: vi.fn(),
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/ui/moyai/" }));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({
    isLoading: false,
    isAuthorized: true,
    userRole: state.role,
    isViewOnly: state.viewOnly,
    userId: "test-user",
    accessToken: "test-session",
  }),
}));
vi.mock("@/app/(dashboard)/hooks/useIsOrgAdmin", () => ({ default: () => false }));
vi.mock("@/app/(dashboard)/hooks/teams/useTeams", () => ({ useTeams: () => ({ data: [], isLoading: false }) }));
vi.mock("@/app/(dashboard)/hooks/uiSettings/useUISettings", () => ({
  useUISettings: () => ({ data: { values: {} }, isPending: false, isLoading: false, refetch: vi.fn() }),
}));
vi.mock("@/contexts/PluginModeContext", () => ({
  PluginModeProvider: ({ children }: { children: ReactNode }) => {
    state.pluginProvider();
    return children;
  },
}));
vi.mock("@/contexts/ThemeContext", () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/navbar", () => ({ default: () => <nav>Gateway navigation</nav> }));
vi.mock("@/components/moyai/MoyaiLanding", () => ({ default: () => <h1>Moyai administrator setup</h1> }));
vi.mock("@/components/networking", () => ({ startMoyaiQuickConnect: state.quickConnect }));

beforeEach(() => {
  state.role = "Internal User";
  state.viewOnly = false;
  state.pluginProvider.mockClear();
  state.quickConnect.mockClear();
});

describe("standalone Moyai route authorization", () => {
  it.each(["Internal User", "Org Admin", "proxy_admin_viewer"])(
    "blocks direct access for %s before plugin or connect setup mounts",
    (role) => {
      state.role = role;
      renderWithProviders(<MoyaiPage />);
      expect(screen.getByText("Access required")).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Moyai administrator setup" })).not.toBeInTheDocument();
      expect(state.pluginProvider).not.toHaveBeenCalled();
      expect(state.quickConnect).not.toHaveBeenCalled();
    },
  );

  it("keeps setup available to a writable proxy administrator", () => {
    state.role = "Admin";
    renderWithProviders(<MoyaiPage />);
    expect(screen.getByRole("heading", { name: "Moyai administrator setup" })).toBeInTheDocument();
    expect(state.pluginProvider).toHaveBeenCalled();
  });
});
