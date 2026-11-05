import { describe, expect, it } from "vitest";
import { canAccessDashboardRoute, canUseAdminControlPlane, type DashboardAccessContext } from "./dashboardAccess";
import { effectiveSessionRole } from "./roles";

const customer: DashboardAccessContext = {
  userRole: "Internal User",
  isViewOnly: false,
  isTeamAdmin: false,
  isOrgAdmin: false,
};

describe("dashboard role boundaries", () => {
  it.each([
    ["proxy_admin", false, true],
    ["Admin", false, true],
    ["proxy_admin", true, false],
    ["proxy_admin_viewer", false, false],
    ["Internal User", false, false],
    ["Org Admin", false, false],
  ] as const)("guards the request-policy route for %s, viewer=%s", (userRole, isViewOnly, expected) => {
    expect(canAccessDashboardRoute("request-policies", { ...customer, userRole, isViewOnly })).toBe(expected);
  });
  it("keeps a native customer on ordinary portal routes after session role normalization", () => {
    const context = { ...customer, userRole: effectiveSessionRole("customer") };
    expect(canAccessDashboardRoute("organization-finance", context)).toBe(true);
    expect(canAccessDashboardRoute("wallet", context)).toBe(true);
    expect(canUseAdminControlPlane(context)).toBe(false);
    for (const page of [
      "wallet-admin",
      "model-pricing",
      "models-and-endpoints",
      "organization-consumption",
      "users",
      "teams",
      "admin-panel",
    ]) {
      expect(canAccessDashboardRoute(page, context)).toBe(false);
    }
  });
  it.each([
    "",
    "home",
    "api-keys",
    "model-catalog",
    "playground",
    "usage",
    "logs",
    "wallet",
    "billing",
    "reports",
    "organizations",
    "operation-audit",
    "api-reference",
    "change-password",
  ])("allows a customer's personal %s page", (page) => expect(canAccessDashboardRoute(page, customer)).toBe(true));
  it.each([
    "models-and-endpoints",
    "moyai",
    "channels",
    "channel-details",
    "resource-pools",
    "wallet-admin",
    "model-pricing",
    "business-labels",
    "audit-delivery",
    "organization-consumption",
    "agents",
    "workflows",
    "memory",
    "mcp-servers",
    "guardrails",
    "policies",
    "search-tools",
    "vector-stores",
    "cost-optimization",
    "lens",
    "teams",
    "users",
    "budgets",
    "access-groups",
    "transform-request",
    "router-settings",
    "logging-and-alerts",
    "admin-panel",
    "ui-theme",
    "caching",
    "model-hub-table",
    "future-admin-page",
  ])("does not grant a customer the %s page through UI configuration", (page) => {
    expect(canAccessDashboardRoute(page, { ...customer, enabledPagesInternalUsers: [page] })).toBe(false);
  });
  it("keeps configured page limits and the existing setting names", () => {
    expect(canAccessDashboardRoute("usage", { ...customer, enabledPagesInternalUsers: ["new_usage"] })).toBe(true);
    expect(canAccessDashboardRoute("model-catalog", { ...customer, enabledPagesInternalUsers: ["models"] })).toBe(true);
    expect(canAccessDashboardRoute("logs", { ...customer, enabledPagesInternalUsers: ["new_usage"] })).toBe(false);
    expect(canAccessDashboardRoute("change-password", { ...customer, enabledPagesInternalUsers: [] })).toBe(true);
  });
  it("preserves delegated team pages without granting gateway configuration", () => {
    const teamAdmin = { ...customer, isTeamAdmin: true, enableProjectsUI: true };
    expect(canAccessDashboardRoute("teams", teamAdmin)).toBe(true);
    expect(canAccessDashboardRoute("projects", teamAdmin)).toBe(true);
    expect(canAccessDashboardRoute("agents", teamAdmin)).toBe(false);
    expect(canAccessDashboardRoute("agents", { ...teamAdmin, allowAgentsForTeamAdmins: true })).toBe(true);
    expect(canAccessDashboardRoute("resource-pools", teamAdmin)).toBe(false);
  });
  it("keeps organization management scoped away from proxy management", () => {
    const organizationAdmin = { ...customer, userRole: "Org Admin", isOrgAdmin: true };
    expect(canAccessDashboardRoute("organizations", organizationAdmin)).toBe(true);
    expect(canAccessDashboardRoute("users", organizationAdmin)).toBe(true);
    expect(canAccessDashboardRoute("admin-panel", organizationAdmin)).toBe(false);
    expect(canAccessDashboardRoute("wallet-admin", organizationAdmin)).toBe(false);
    expect(canAccessDashboardRoute("model-pricing", organizationAdmin)).toBe(false);
  });
  it("preserves proxy management and viewer cost restrictions", () => {
    expect(canAccessDashboardRoute("models-and-endpoints", { ...customer, userRole: "Admin" })).toBe(true);
    expect(canAccessDashboardRoute("playground", { ...customer, userRole: "Admin", isViewOnly: true })).toBe(false);
    expect(canUseAdminControlPlane(customer)).toBe(false);
    expect(canUseAdminControlPlane({ userRole: "Admin", isViewOnly: false })).toBe(true);
    expect(canUseAdminControlPlane({ userRole: "proxy_admin_viewer", isViewOnly: false })).toBe(false);
    expect(canAccessDashboardRoute("moyai", { ...customer, userRole: "Admin", isViewOnly: true })).toBe(false);
  });
  it.each([
    "channels",
    "channel-details",
    "resource-pools",
    "wallet-admin",
    "model-pricing",
    "business-labels",
    "audit-delivery",
    "organization-consumption",
  ])("does not expose %s to a proxy viewer", (page) => {
    expect(canAccessDashboardRoute(page, { ...customer, userRole: "proxy_admin_viewer" })).toBe(false);
    expect(canAccessDashboardRoute(page, { ...customer, userRole: "Admin", isViewOnly: true })).toBe(false);
    expect(canAccessDashboardRoute(page, { ...customer, userRole: "Admin" })).toBe(true);
  });
  it.each(["Admin", "proxy_admin"])("allows global model pricing only for the writable proxy role %s", (userRole) => {
    expect(canAccessDashboardRoute("model-pricing", { ...customer, userRole })).toBe(true);
    expect(canAccessDashboardRoute("model-pricing", { ...customer, userRole, isViewOnly: true })).toBe(false);
  });
  it("fails closed for unresolved roles", () => {
    expect(canAccessDashboardRoute("home", { ...customer, userRole: "" })).toBe(false);
    expect(canAccessDashboardRoute("channels", { ...customer, userRole: "Unknown Role" })).toBe(false);
  });
  it.each(["Internal User", "Org Admin", "Admin"])(
    "allows the delegated finance shell to writable %s without opening admin consumption",
    (userRole) => {
      expect(canAccessDashboardRoute("organization-finance", { ...customer, userRole })).toBe(true);
      if (userRole !== "Admin")
        expect(canAccessDashboardRoute("organization-consumption", { ...customer, userRole })).toBe(false);
    },
  );
  it.each(["proxy_admin_viewer", "internal_user_viewer"])(
    "blocks the delegated finance shell for readonly %s",
    (userRole) => {
      expect(canAccessDashboardRoute("organization-finance", { ...customer, userRole })).toBe(false);
      expect(
        canAccessDashboardRoute("organization-finance", { ...customer, userRole: "Admin", isViewOnly: true }),
      ).toBe(false);
    },
  );
});
