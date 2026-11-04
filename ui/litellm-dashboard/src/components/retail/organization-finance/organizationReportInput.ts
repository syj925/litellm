import { RetailRequestError } from "../api";
import { managementPageParser } from "../management/ManagementShared";
import type {
  CreateOrganizationReport,
  OrganizationConsumption,
  OrganizationReportJob,
  OrganizationReportState,
} from "./types";

export const organizationExportPageParser = managementPageParser;
export const organizationReportLabels: Record<OrganizationReportState, string> = {
  queued: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
  revoked: "Access revoked",
  expired: "Expired",
};

export function organizationReportPending(job: Pick<OrganizationReportJob, "state">): boolean {
  return job.state === "queued" || job.state === "processing";
}

export function organizationReportPayload(
  preview: OrganizationConsumption | undefined,
  organizationId: string,
): CreateOrganizationReport | null {
  if (!preview?.organization_enabled || preview.organization_id !== organizationId) return null;
  return { start_at: preview.start, end_at: preview.end, currency: "USD", timezone: "Asia/Shanghai" };
}

export function organizationReportRefreshNeeded(error: Error): boolean {
  return error instanceof RetailRequestError && [404, 409, 410].includes(error.status);
}

export function organizationReportError(error: Error): string {
  if (!(error instanceof RetailRequestError))
    return "The result could not be confirmed. Reload tasks or retry the same request.";
  if ([401, 403].includes(error.status))
    return "Export access could not be verified. Reload the preview to check your current access.";
  if (error.status === 404) return "This organization or export is unavailable. Reload the preview and task list.";
  if (error.status === 410) return "This export has expired. Create a new fixed snapshot to download a CSV.";
  if (error.status === 409) return "The export is not ready or the request changed. Reload tasks before retrying.";
  if (error.status === 422)
    return "This export could not be created. Apply a valid window of up to 31 days and 10,000 requests, then retry.";
  if (error.status === 429) return "The export queue is busy. Wait for a task to finish, then retry the same request.";
  return "The export request could not be completed. Reload tasks and try again.";
}
