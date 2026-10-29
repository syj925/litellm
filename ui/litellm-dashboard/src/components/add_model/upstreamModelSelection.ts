export interface UpstreamModelMapping {
  public_name: string;
  litellm_model: string;
}

const specialModels = new Set(["custom", "all-wildcard"]);

export function uniqueModelIds(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter((value) => value.length > 0))];
}

export function filterModelIds(models: readonly string[], query: string): string[] {
  const normalized = query.trim().toLowerCase();
  return models.filter((model) => model.toLowerCase().includes(normalized));
}

function isMapping(value: unknown): value is UpstreamModelMapping {
  return (
    typeof value === "object" &&
    value !== null &&
    "public_name" in value &&
    "litellm_model" in value &&
    typeof value.public_name === "string" &&
    typeof value.litellm_model === "string" &&
    value.public_name.trim().length > 0 &&
    value.litellm_model.trim().length > 0 &&
    !specialModels.has(value.litellm_model)
  );
}

export function mergeUpstreamModels(
  currentModels: unknown,
  currentMappings: unknown,
  selectedIds: readonly string[],
  provider: string,
): { model: string[]; model_mappings: UpstreamModelMapping[] } {
  const mappings = Array.isArray(currentMappings) ? currentMappings.filter(isMapping) : [];
  const modelValues = (Array.isArray(currentModels) ? currentModels : [currentModels]).filter(
    (value): value is string => typeof value === "string" && value.trim().length > 0 && !specialModels.has(value),
  );
  const prefix = `${provider}/`;
  const upstreamId = (model: string) => (model.startsWith(prefix) ? model.slice(prefix.length) : model);
  const mappedIds = new Set(mappings.map((mapping) => upstreamId(mapping.litellm_model)));
  const unmapped = modelValues.filter((model) => !mappedIds.has(upstreamId(model)));
  const existing = [...mappings, ...unmapped.map((model) => ({ public_name: model, litellm_model: model }))];
  const existingIds = new Set(
    existing.map((mapping) =>
      mapping.litellm_model.startsWith(prefix) ? mapping.litellm_model.slice(prefix.length) : mapping.litellm_model,
    ),
  );
  const added = uniqueModelIds(selectedIds)
    .filter((id) => !existingIds.has(id))
    .map((id) => ({
      public_name: id,
      litellm_model: `${prefix}${id}`,
    }));
  const model_mappings = [...existing, ...added];
  return { model: model_mappings.map((mapping) => mapping.litellm_model), model_mappings };
}
