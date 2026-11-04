import { createParser, parseAsIsoDateTime } from "nuqs";
import { RetailRequestError } from "../api";
import { managementPageParser, managementTextParser } from "../management/ManagementShared";
import type { BillingOrganization, ConsumptionState } from "./types";

const exactOrganizationParser = createParser({
  parse: (value) => (value.length > 0 && value.length <= 191 && !/\s/.test(value) ? value : null),
  serialize: String,
}).withDefault("");

export const consumptionParsers = {
  organization: exactOrganizationParser,
  start: parseAsIsoDateTime,
  end: parseAsIsoDateTime,
  page: managementPageParser,
};
export const organizationPickerParsers = { org_q: managementTextParser, org_page: managementPageParser };
export const assignmentPickerParsers = { billing_org_q: managementTextParser, billing_org_page: managementPageParser };

export function consumptionWindow(start: Date | null, end: Date | null): string | null {
  if (!start && !end) return null;
  if (!start || !end) return "Choose both a start and an end time.";
  const duration = end.getTime() - start.getTime();
  if (!Number.isFinite(duration) || duration <= 0) return "The end time must be after the start time.";
  if (duration > 31 * 24 * 60 * 60 * 1000) return "Choose a window of 31 days or less.";
  return null;
}

export function billingPayload(selection: string, version: number) {
  if (!Number.isSafeInteger(version) || version < 0) return null;
  if (selection === "clear") return { organization_id: null, expected_version: version };
  if (selection.startsWith("organization:") && selection.length > 13) {
    return { organization_id: selection.slice(13), expected_version: version };
  }
  return null;
}

export function assignmentLabel(state: BillingOrganization["state"]): string {
  const labels = {
    unassigned: "No billing organization",
    active: "Active for new requests",
    organization_disabled: "Inactive — organization disabled",
    membership_inactive: "Inactive — membership unavailable",
  };
  return labels[state];
}

export function consumptionLabel(state: ConsumptionState): string {
  const labels = {
    settled: "Settled",
    released: "Released",
    pending: "Pending",
    unknown: "Unknown",
    conflict: "Conflict",
  };
  return labels[state];
}

export function financeAccessLost(error: Error | null, missingIsUnavailable = false): boolean {
  if (!(error instanceof RetailRequestError)) return false;
  if ([401, 403].includes(error.status)) return true;
  return missingIsUnavailable && error.status === 404;
}

export type FinanceErrorContext = "assignment" | "organizations" | "consumption";

const validationMessages: Record<FinanceErrorContext, string> = {
  assignment:
    "This organization is no longer eligible for this account. Reload the assignment and choose an active membership.",
  organizations: "The organization list could not be loaded. Adjust the search and reload.",
  consumption:
    "This window could not be loaded. Choose valid dates within 31 days and narrow the window to at most 10,000 requests.",
};

export function financeError(error: Error, context: FinanceErrorContext): string {
  if (!(error instanceof RetailRequestError))
    return "The result could not be confirmed. Reload, or retry the same change.";
  if (error.status === 409) return "This assignment changed. Reload before saving again.";
  if ([401, 403, 404].includes(error.status))
    return "Access could not be verified. Reload to check your current access.";
  if (error.status === 422) return validationMessages[context];
  return "The request could not be completed. Reload and try again.";
}
