import { RetailRequestError } from "../api";
import type { ChannelConnectionPatch } from "./types";

export function channelConnectionPatch(
  changeApiBase: boolean,
  apiBase: string,
  apiKey: string,
): ChannelConnectionPatch | null {
  const base = changeApiBase ? apiBase.trim() : "";
  const key = apiKey.trim();
  if (!base && !key) return null;
  return { ...(base ? { api_base: base } : {}), ...(key ? { api_key: key } : {}) };
}

export function channelConnectionSaveError(error: Error): string {
  if (error instanceof RetailRequestError) {
    if (error.status === 409) return "Channel changed. Reload before saving.";
    if (error.status === 401 || error.status === 403)
      return "Administrator access is required to update this connection.";
    if (error.status === 400 || error.status === 422)
      return "The connection update was rejected. Review the URL, provider support and selected deployments.";
  }
  return "The connection could not be updated. Reload this channel and try again.";
}
