import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";
import { ApiError, createApiClient, type RequestOptions } from "@/lib/http/client";
import { RetailRequestError } from "../api";

export async function downloadRetailReport(reportId: string, accessToken: string): Promise<void> {
  const client = createApiClient({ getBaseUrl: getProxyBaseUrl, getAuthHeaderName: getGlobalLitellmHeaderName });
  const options: RequestOptions = {
    accessToken,
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "text/csv" },
  };
  let file: Blob;
  try {
    file = await client.getBlob(`/retail/reports/${encodeURIComponent(reportId)}/download`, options);
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    throw new RetailRequestError(
      "This report cannot be downloaded. Refresh its status and check your access.",
      error.status,
    );
  }
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = `wallet-report-${reportId.replace(/[^a-zA-Z0-9_-]/g, "_")}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
