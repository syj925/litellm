import { describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen } from "../../../../tests/test-utils";
import { RequestLogsTable } from "./RequestLogsTable";
import type { LogEntry } from "../types";

const session = vi.hoisted(() => ({ role: "Internal User" }));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({ default: () => ({ userRole: session.role }) }));
vi.mock("@/app/(dashboard)/hooks/users/useUsers", () => ({ useUserEmailLookup: () => ({ data: {} }) }));

const props = {
  data: [],
  rowCount: 0,
  isLoading: false,
  isRefreshing: false,
  pagination: { pageIndex: 0, pageSize: 25 },
  onPaginationChange: vi.fn(),
  sorting: [],
  onSortingChange: vi.fn(),
  columnFilters: [],
  onColumnFiltersChange: vi.fn(),
  searchValue: "",
  onSearchChange: vi.fn(),
  onRefresh: vi.fn(),
  onRowClick: vi.fn(),
  onKeyHashClick: vi.fn(),
  onSessionClick: vi.fn(),
  teams: [],
  logsWindow: "24h" as const,
};

describe("request log cost columns", () => {
  it.each(["internal_user", "internal_user_viewer", "customer", "team_admin", "org_admin", "unknown"])(
    "hides private upstream routing, key hashes and provider logos from %s",
    (role) => {
      session.role = role;
      const row = {
        request_id: "customer-request",
        model: "public-model",
        custom_llm_provider: "anthropic",
        metadata: { user_api_key: "private-key-hash", retail_resource_pool_name: "private-pool" },
      } as LogEntry;
      renderWithProviders(<RequestLogsTable {...props} data={[row]} rowCount={1} />);
      expect(screen.getByText("public-model")).toBeInTheDocument();
      expect(screen.queryByRole("columnheader", { name: "Key Hash" })).not.toBeInTheDocument();
      expect(screen.queryByRole("columnheader", { name: "Resource pool" })).not.toBeInTheDocument();
      expect(screen.queryByTitle("API integration: anthropic")).not.toBeInTheDocument();
      expect(screen.queryByText(/private-/)).not.toBeInTheDocument();
    },
  );

  it.each(["Internal User", "Org Admin"])("hides native Cost from %s", (role) => {
    session.role = role;
    renderWithProviders(<RequestLogsTable {...props} />);
    expect(screen.queryByRole("columnheader", { name: /Cost/ })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Tokens/ })).toBeInTheDocument();
  });

  it.each(["Admin", "proxy_admin_viewer"])("keeps the native Cost column for %s", (role) => {
    session.role = role;
    renderWithProviders(<RequestLogsTable {...props} />);
    expect(screen.getByRole("columnheader", { name: /Cost/ })).toBeInTheDocument();
  });
});
