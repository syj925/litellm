import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";
import { ApiError, createApiClient, type RequestOptions } from "@/lib/http/client";
import { RetailRequestError } from "../../api";

export async function downloadDelegatedReport(organization: string, id: string, accessToken: string): Promise<void> {
  const client = createApiClient({ getBaseUrl: getProxyBaseUrl, getAuthHeaderName: getGlobalLitellmHeaderName });
  const options: RequestOptions = {
    accessToken,
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "text/csv" },
  };
  let blob: Blob;
  try {
    blob = await client.getBlob(
      `/retail/organizations/${encodeURIComponent(organization)}/finance/reports/${encodeURIComponent(id)}/download`,
      options,
    );
  } catch (error) {
    if (error instanceof ApiError) throw new RetailRequestError("Export unavailable", error.status);
    throw error;
  }
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `organization-finance-${id.replace(/[^a-zA-Z0-9_-]/g, "_")}.csv`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
