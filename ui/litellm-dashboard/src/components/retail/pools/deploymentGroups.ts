import type { PoolBinding, PoolDeployment } from "./types";

export interface BindingDraft extends Omit<PoolBinding, "weight" | "priority"> {
  weight: string;
  priority: string;
}

export interface DeploymentGroup {
  key: string;
  channelId: string | null;
  name: string;
  poolId: string | null;
  poolName: string | null;
  items: PoolDeployment[];
}

export function deploymentGroups(deployments: PoolDeployment[]): DeploymentGroup[] {
  return deployments.reduce<DeploymentGroup[]>((groups, item) => {
    const key = item.channel_id ? `channel:${item.channel_id}` : `deployment:${item.deployment_id}`;
    const existing = groups.find((group) => group.key === key);
    if (existing) {
      return groups.map((group) => (group.key === key ? { ...group, items: [...group.items, item] } : group));
    }
    return [
      ...groups,
      {
        key,
        channelId: item.channel_id ?? null,
        name: item.channel_name || "Unnamed deployment",
        poolId: item.pool_id ?? null,
        poolName: item.pool_name ?? null,
        items: [item],
      },
    ];
  }, []);
}

export function matchingDeploymentGroups(groups: DeploymentGroup[], search: string): DeploymentGroup[] {
  const query = search.trim().toLowerCase();
  return groups.filter((group) =>
    `${group.name} ${group.channelId ?? ""} ${group.poolName ?? ""} ${group.items
      .map((item) => `${item.model} ${item.provider} ${item.deployment_id} ${item.credential_name ?? ""}`)
      .join(" ")}`
      .toLowerCase()
      .includes(query),
  );
}

export function hasBinding(bindings: BindingDraft[], item: PoolDeployment): boolean {
  return bindings.some((binding) => binding.model === item.model && binding.deployment_id === item.deployment_id);
}

export function addDeploymentBindings(bindings: BindingDraft[], items: PoolDeployment[]): BindingDraft[] {
  return [
    ...bindings,
    ...items
      .filter((item) => !hasBinding(bindings, item))
      .map((item) => ({ deployment_id: item.deployment_id, model: item.model, weight: "1", priority: "0" })),
  ];
}
