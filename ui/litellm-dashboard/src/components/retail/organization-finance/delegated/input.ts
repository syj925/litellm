import { canAccessDashboardRoute } from "@/utils/dashboardAccess";
import { RetailRequestError } from "../../api";
import { managementPageParser, managementTextParser } from "../../management/ManagementShared";
import { consumptionParsers } from "../financeInput";
import type { FinanceAccess, GrantUpdate } from "./types";

export const delegatedParsers = {
  ...consumptionParsers,
  scope_q: managementTextParser,
  scope_page: managementPageParser,
  export_page: managementPageParser,
};
export function delegatedActor(userRole: string, isViewOnly: boolean): boolean {
  if (isViewOnly || userRole.toLowerCase().endsWith("viewer")) return false;
  const context = {
    userRole,
    isViewOnly,
    isOrgAdmin: false,
    isTeamAdmin: false,
  };
  return canAccessDashboardRoute("organization-finance", context);
}
export function delegatedAccessLost(error: Error | null): boolean {
  return error instanceof RetailRequestError && [401, 403, 404].includes(error.status);
}
export function delegatedError(error: Error): string {
  if (!(error instanceof RetailRequestError))
    return "The result could not be confirmed. Reload access or retry the same request.";
  if ([401, 403, 404].includes(error.status))
    return "Finance access is unavailable or has changed. Reload access to verify your current permissions.";
  if (error.status === 410) return "This snapshot has expired. Create a new export if you still have export access.";
  if (error.status === 409)
    return "This request belongs to an earlier authorization or is not ready. Reload access before creating a new snapshot.";
  if (error.status === 422) return "Choose a valid window of up to 31 days and 10,000 accepted requests.";
  return "The finance request could not be completed. Reload and try again.";
}
export function verifiedFinanceAccess(
  access: FinanceAccess | undefined,
  organizationId: string,
): access is FinanceAccess & { access_revision: string } {
  return Boolean(access?.can_view && access.organization_id === organizationId && access.access_revision);
}
export function grantPayload(value: string, version: number): GrantUpdate | null {
  if (!Number.isSafeInteger(version) || version < 0) return null;
  if (!["none", "view", "export"].includes(value)) return null;
  return { can_view: value !== "none", can_export: value === "export", expected_version: version };
}
