export interface DashboardAccessContext {
  userRole: string;
  isViewOnly: boolean;
  isTeamAdmin: boolean;
  isOrgAdmin: boolean;
  enabledPagesInternalUsers?: readonly string[] | null;
  enableProjectsUI?: boolean;
  allowAgentsForTeamAdmins?: boolean;
  allowVectorStoresForTeamAdmins?: boolean;
}

const roleKey = (role: string): string => role.toLowerCase().replaceAll(" ", "_");
const proxyRoles = new Set(["admin", "admin_viewer", "proxy_admin", "proxy_admin_viewer"]);
const customerRoles = new Set([
  "internal",
  "internal_user",
  "internal_viewer",
  "internal_user_viewer",
  "app_user",
  "app_owner",
  "demo_app_owner",
]);
const customerPages = new Set([
  "home",
  "api-keys",
  "playground",
  "usage",
  "logs",
  "wallet",
  "billing",
  "reports",
  "organizations",
  "operation-audit",
  "model-catalog",
  "api-reference",
  "learning-resources",
  "change-password",
]);
const pageSettingKeys: Record<string, string> = {
  playground: "llm-playground",
  usage: "new_usage",
  "api-reference": "api_ref",
  "model-catalog": "models",
};
const retailAdminPages = new Set([
  "channels",
  "channel-details",
  "resource-pools",
  "wallet-admin",
  "model-pricing",
  "business-labels",
  "audit-delivery",
  "organization-consumption",
]);

export const isProxyDashboardRole = (role: string): boolean => proxyRoles.has(roleKey(role));

export function canUseAdminControlPlane(context: Pick<DashboardAccessContext, "userRole" | "isViewOnly">): boolean {
  return isProxyDashboardRole(context.userRole) && !context.isViewOnly && !roleKey(context.userRole).endsWith("viewer");
}

function delegatedPageAccess(page: string, context: DashboardAccessContext, orgAdmin: boolean): boolean {
  switch (page) {
    case "teams":
    case "projects":
      return context.isTeamAdmin || orgAdmin;
    case "users":
    case "organizations":
      return orgAdmin;
    case "agents":
      return context.isTeamAdmin && context.allowAgentsForTeamAdmins === true;
    case "vector-stores":
      return context.isTeamAdmin && context.allowVectorStoresForTeamAdmins === true;
    default:
      return false;
  }
}

export function canAccessDashboardRoute(route: string, context: DashboardAccessContext): boolean {
  const page = route || "home";
  if (page === "moyai") return canUseAdminControlPlane(context);
  const viewOnly = context.isViewOnly || roleKey(context.userRole).endsWith("viewer");
  if (viewOnly && (page === "playground" || retailAdminPages.has(page))) return false;
  if (isProxyDashboardRole(context.userRole)) return true;
  const orgAdmin = context.isOrgAdmin || roleKey(context.userRole) === "org_admin";
  if (!customerRoles.has(roleKey(context.userRole)) && !orgAdmin) return false;
  if (page === "change-password") return true;
  const allowed = customerPages.has(page) || delegatedPageAccess(page, context, orgAdmin);
  if (!allowed || (page === "projects" && !context.enableProjectsUI)) return false;
  const enabled = context.enabledPagesInternalUsers;
  return enabled == null || enabled.includes(page) || enabled.includes(pageSettingKeys[page] ?? page);
}
