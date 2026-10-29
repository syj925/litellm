import type { PoolDeployment } from "../pools/types";

export interface ChannelDeployment extends PoolDeployment {
  connection_id?: string;
  endpoint_host?: string | null;
  channel_id?: string | null;
  channel_name?: string | null;
  pool_id?: string | null;
  pool_name?: string | null;
}

export function groupConnections(deployments: ChannelDeployment[]) {
  const keys = [...new Set(deployments.map((item) => item.connection_id || item.deployment_id))];
  return keys.map((key) => {
    const items = deployments.filter((item) => (item.connection_id || item.deployment_id) === key);
    return { id: key, label: items[0].endpoint_host || items[0].credential_name || "Unnamed connection", items };
  });
}

export function toggleDeployments(selected: string[], ids: string[], checked: boolean): string[] {
  return checked ? [...new Set([...selected, ...ids])] : selected.filter((id) => !ids.includes(id));
}
