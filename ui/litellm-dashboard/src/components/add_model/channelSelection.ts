import type { ChannelSelection, NamedChannel } from "../retail/channels/types";
import type { ResourcePool } from "../retail/pools/types";

export function parseChannelSelection(value: unknown): ChannelSelection | null {
  if (typeof value !== "object" || value === null) return null;
  if (
    "mode" in value &&
    value.mode === "new" &&
    "name" in value &&
    typeof value.name === "string" &&
    "pool_id" in value &&
    typeof value.pool_id === "string"
  ) {
    return { mode: "new", name: value.name, pool_id: value.pool_id };
  }
  if ("mode" in value && value.mode === "existing" && "channel_id" in value && typeof value.channel_id === "string") {
    return { mode: "existing", channel_id: value.channel_id };
  }
  return null;
}

export function channelSelectionError(value: unknown, channels: NamedChannel[], pools: ResourcePool[]): string | null {
  const selection = parseChannelSelection(value);
  if (!selection) return "Choose a named channel and resource pool";
  if (selection.mode === "new") {
    if (!selection.name.trim() || selection.name.trim().length > 100)
      return "Enter a channel name with 1 to 100 characters";
    if (!pools.some((pool) => pool.id === selection.pool_id)) return "Choose a resource pool for this channel";
    return null;
  }
  const channel = channels.find((item) => item.id === selection.channel_id);
  if (!channel || channel.status !== "active") return "Choose an available channel";
  if (!pools.some((pool) => pool.id === channel.pool_id)) return "This channel's resource pool is unavailable";
  return null;
}
