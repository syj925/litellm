export interface ResourceGrant {
  pool_id: string;
  models: "*" | string[];
}

export type ResourceRoutingPolicy =
  | { mode: "auto" }
  | { mode: "fixed"; pool_id: string }
  | { mode: "custom"; grants: ResourceGrant[] };

export interface KeyResourcePool {
  id: string;
  name: string;
  enabled: boolean;
  models: string[];
  allowed_models: string[];
  source_kind?: string;
}

export interface KeyResourceCatalog {
  routing_enabled: boolean;
  items: KeyResourcePool[];
}

export function resourcePolicyPoolModels(
  policy: ResourceRoutingPolicy | null,
  pools: KeyResourcePool[],
): { pool: KeyResourcePool; models: string[] }[] {
  if (policy === null) return [];
  return pools.flatMap((pool) => {
    if (!pool.enabled || !pool.allowed_models.length) return [];
    if (policy.mode === "fixed" && policy.pool_id !== pool.id) return [];
    const grant = policy.mode === "custom" ? policy.grants.find((item) => item.pool_id === pool.id) : undefined;
    if (policy.mode === "custom" && !grant) return [];
    const grantModels = grant?.models;
    const models = pool.allowed_models.filter(
      (model) => grantModels === undefined || grantModels === "*" || grantModels.includes(model),
    );
    return models.length ? [{ pool, models: [...new Set(models)] }] : [];
  });
}

export function resourcePolicyModels(policy: ResourceRoutingPolicy | null, pools: KeyResourcePool[]): string[] {
  return [...new Set(resourcePolicyPoolModels(policy, pools).flatMap((item) => item.models))];
}

export function resourceModelHelp(scoped: boolean, ready: boolean, failed: boolean, count: number): string | undefined {
  if (failed) return "Resource access could not be verified. Retry before selecting models.";
  if (!ready) return "Loading account-authorized models...";
  if (!scoped) return undefined;
  if (!count) return "No models are available in this resource scope. Choose an available pool or model grant.";
  return "Optional. Leave empty to allow all models in the selected resource scope.";
}

export function resourcePolicyError(policy: ResourceRoutingPolicy | null, pools: KeyResourcePool[]): string | null {
  if (policy === null || policy.mode === "auto") return null;
  const allowed = (id: string) =>
    pools.find((pool) => pool.id === id && pool.enabled && pool.allowed_models.length > 0);
  if (policy.mode === "fixed") return allowed(policy.pool_id) ? null : "Choose an available account-authorized pool.";
  if (!policy.grants.length) return "Select at least one pool and one allowed model for custom routing.";
  if (new Set(policy.grants.map((grant) => grant.pool_id)).size !== policy.grants.length)
    return "Each resource pool may appear only once.";
  for (const grant of policy.grants) {
    const pool = allowed(grant.pool_id);
    if (!pool) return "A selected pool is disabled or is no longer authorized for this account.";
    if (
      grant.models !== "*" &&
      (!grant.models.length || grant.models.some((model) => !pool.allowed_models.includes(model)))
    )
      return "Select only models authorized for each individual pool.";
  }
  return null;
}

export function readResourcePolicy(metadata: unknown): ResourceRoutingPolicy | null {
  if (typeof metadata !== "object" || metadata === null || !("retail_resource_policy" in metadata)) return null;
  const policy = metadata.retail_resource_policy;
  if (typeof policy !== "object" || policy === null || !("mode" in policy)) return null;
  if (policy.mode === "auto") return { mode: "auto" };
  if (policy.mode === "fixed" && "pool_id" in policy && typeof policy.pool_id === "string")
    return { mode: "fixed", pool_id: policy.pool_id };
  if (policy.mode === "custom" && "grants" in policy && Array.isArray(policy.grants)) {
    const grants = policy.grants.filter(
      (grant): grant is ResourceGrant =>
        typeof grant === "object" &&
        grant !== null &&
        typeof grant.pool_id === "string" &&
        (grant.models === "*" ||
          (Array.isArray(grant.models) && grant.models.every((model: unknown) => typeof model === "string"))),
    );
    return { mode: "custom", grants };
  }
  return null;
}

export function withResourcePolicy(metadata: unknown, policy: ResourceRoutingPolicy | null): Record<string, unknown> {
  const source: unknown = typeof metadata === "string" ? JSON.parse(metadata || "{}") : metadata;
  const object =
    source && typeof source === "object" && !Array.isArray(source) ? (source as Record<string, unknown>) : {};
  const kept = Object.fromEntries(Object.entries(object).filter(([key]) => key !== "retail_resource_policy"));
  return policy === null ? kept : { ...kept, retail_resource_policy: policy };
}

export function resourcePolicySummary(policy: ResourceRoutingPolicy | null): string {
  if (policy === null) return "Legacy routing, used only while account pool routing is disabled.";
  if (policy.mode === "auto") return "Automatic: account-authorized pools and their allowed models.";
  if (policy.mode === "fixed") return `Fixed pool: ${policy.pool_id}. No fallback to other pools.`;
  return policy.grants
    .map((grant) => `${grant.pool_id}: ${grant.models === "*" ? "all allowed models" : grant.models.join(", ")}`)
    .join("; ");
}

export function resourceRoutingCurl(baseUrl: string): string {
  return [
    `curl '${baseUrl.replace(/\/$/, "")}/v1/messages' \\`,
    "  -H 'Authorization: Bearer YOUR_VIRTUAL_KEY' \\",
    "  -H 'Content-Type: application/json' \\",
    "  -H 'anthropic-version: 2023-06-01' \\",
    '  -d \'{"model":"YOUR_PUBLIC_MODEL","max_tokens":32,"messages":[{"role":"user","content":"Hello"}]}\'',
  ].join("\n");
}
