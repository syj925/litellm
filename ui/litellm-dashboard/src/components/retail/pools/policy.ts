import type { PoolGrant, ResourcePool } from "./types";

export function updatePoolPair(
  grants: PoolGrant[],
  pool: Pick<ResourcePool, "id" | "models">,
  model: string,
  checked: boolean,
): PoolGrant[] {
  const existing = grants.find((grant) => grant.pool_id === pool.id);
  const models = existing?.models === "*" ? pool.models : existing?.models ?? [];
  const next = checked ? [...new Set([...models, model])] : models.filter((item) => item !== model);
  const others = grants.filter((grant) => grant.pool_id !== pool.id);
  return next.length ? [...others, { pool_id: pool.id, models: next }] : others;
}
