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
  const selected = [...new Set(selectedModels)].map((value) => ({
    value,
    label: modelDisplayName(value, selectedProvider),
  }));
  const selectedNames = new Set(selected.map((option) => option.label));
  const catalog = new Map<string, { value: string; label: string }>();
  for (const value of catalogModels) {
    const label = modelDisplayName(value, selectedProvider);
    if (!catalog.has(label)) catalog.set(label, { value, label });
  }
  const options = [...selected, ...[...catalog.values()].filter((option) => !selectedNames.has(option.label))];
  return { options, selectAllValues: options.map((option) => option.value), catalogCount: catalog.size };
}
