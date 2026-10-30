export interface RecordedResourceRoute {
  poolId: string | null;
  poolName: string | null;
  sourceKind: string | null;
}

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function recordedResourceRoute(metadata: unknown): RecordedResourceRoute {
  const values = objectValue(metadata);
  const nested = objectValue(values.litellm_metadata);
  const text = (key: string): string | null => {
    const value = values[key] ?? nested[key];
    return typeof value === "string" && value.trim() ? value : null;
  };
  return {
    poolId: text("retail_resource_pool_id"),
    poolName: text("retail_resource_pool_name"),
    sourceKind: text("retail_resource_source_kind"),
  };
}
