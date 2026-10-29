export interface PoolBinding {
  deployment_id: string;
  model: string;
  weight: number;
  priority: number;
}

export interface ResourcePool {
  id: string;
  name: string;
  source_kind: string;
  description: string;
  enabled: boolean;
  visibility: "public" | "restricted";
  priority: number;
  default_discount: string;
  version: number;
  bindings: PoolBinding[];
  models: string[];
}

export interface PoolDeployment {
  deployment_id: string;
  model: string;
  provider: string;
  credential_name: string | null;
  channel_id?: string | null;
  channel_name?: string | null;
  pool_id?: string | null;
  pool_name?: string | null;
  endpoint_host?: string | null;
  connection_id?: string | null;
}

export interface PoolSettings {
  routing_enabled: boolean;
  version: number;
  unassigned_models: string[];
}

export interface PoolGrant {
  pool_id: string;
  models: "*" | string[];
}

export interface PoolAccess {
  mode: "inherit" | "custom";
  grants: PoolGrant[];
  effective_grants: PoolGrant[];
}

export interface PoolCatalog {
  items: (Omit<ResourcePool, "bindings"> & { allowed_models?: string[] })[];
  policy_modes?: string[];
  routing_enabled?: boolean;
}

export const sourceKinds = [
  { value: "bedrock", label: "AWS Bedrock" },
  { value: "anthropic", label: "Anthropic" },
  { value: "vertex", label: "Vertex AI" },
  { value: "anthropic_subscribe", label: "Anthropic Subscribe" },
  { value: "custom", label: "Custom source" },
] as const;

export function sourceKindLabel(value: string): string {
  return sourceKinds.find((item) => item.value === value)?.label ?? value;
}

export const poolTemplates = [
  { name: "AWS Bedrock Claude", source_kind: "bedrock" },
  { name: "Anthropic", source_kind: "anthropic" },
  { name: "Vertex AI Claude", source_kind: "vertex" },
  { name: "Anthropic Subscribe", source_kind: "anthropic_subscribe" },
] as const;

export function poolIdFromName(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 80);
}
