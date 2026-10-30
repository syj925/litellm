import { Providers, provider_map } from "../provider_info_helpers";

const adapters = new Set(Object.values(provider_map));

export function modelDisplayName(model: string, selectedProvider: unknown): string {
  if (typeof selectedProvider !== "string") return model;
  const mappedAdapter = provider_map[selectedProvider];
  const providerKey = mappedAdapter
    ? undefined
    : Object.entries(Providers).find(([, name]) => name === selectedProvider)?.[0];
  const adapter =
    mappedAdapter ??
    (providerKey ? provider_map[providerKey] : undefined) ??
    (adapters.has(selectedProvider) ? selectedProvider : undefined);
  const prefix = adapter ? `${adapter}/` : null;
  return prefix && model.startsWith(prefix) && model.length > prefix.length ? model.slice(prefix.length) : model;
}

export function buildModelChoices(
  catalogModels: readonly string[],
  selectedModels: readonly string[],
  selectedProvider: unknown,
): { options: { value: string; label: string }[]; selectAllValues: string[]; catalogCount: number } {
  const values = [...new Set([...selectedModels, ...catalogModels])];
  const options = values.map((value) => ({
    value,
    label: modelDisplayName(value, selectedProvider),
  }));
  return { options, selectAllValues: values, catalogCount: new Set(catalogModels).size };
}
