import { parseAsArrayOf, parseAsFloat, parseAsInteger, parseAsString, parseAsStringLiteral } from "nuqs";
import { getModelIdentity } from "./modelIdentity";
import { catalogDetailTabs, modelPrice, type CatalogModel, type CatalogPriceBasis } from "./types";

export const capabilityOptions = [
  { id: "tools", label: "Tool calling" },
  { id: "reasoning", label: "Reasoning" },
  { id: "vision", label: "Vision" },
  { id: "prompt_caching", label: "Prompt caching" },
  { id: "structured_output", label: "Structured output" },
  { id: "audio_input", label: "Audio input" },
  { id: "audio_output", label: "Audio output" },
  { id: "pdf_input", label: "PDF input" },
] as const;
const capabilityIds = capabilityOptions.map(({ id }) => id);

export const catalogParsers = {
  q: parseAsString.withDefault(""),
  authors: parseAsArrayOf(parseAsString).withDefault([]),
  pools: parseAsArrayOf(parseAsString).withDefault([]),
  capabilities: parseAsArrayOf(parseAsStringLiteral(capabilityIds)).withDefault([]),
  input: parseAsArrayOf(parseAsString).withDefault([]),
  context: parseAsInteger.withDefault(0),
  max_price: parseAsFloat.withDefault(-1),
  availability: parseAsStringLiteral(["all", "available"]).withDefault("all"),
  mode: parseAsString.withDefault("all"),
  sort: parseAsStringLiteral(["name", "context", "input_price", "output_price"]).withDefault("name"),
  view: parseAsStringLiteral(["list", "table"]).withDefault("list"),
  price: parseAsStringLiteral(["reference", "customer"]).withDefault("reference"),
  model: parseAsString.withDefault(""),
  model_tab: parseAsStringLiteral(catalogDetailTabs).withDefault("overview"),
  compare: parseAsArrayOf(parseAsString).withDefault([]),
};

export interface CatalogFilters {
  q: string;
  authors: string[];
  pools: string[];
  capabilities: (typeof capabilityIds)[number][];
  input: string[];
  context: number;
  max_price: number;
  availability: "all" | "available";
  mode: string;
  sort: "name" | "context" | "input_price" | "output_price";
  price: CatalogPriceBasis;
}

export function filterCatalog(models: CatalogModel[], filters: CatalogFilters): CatalogModel[] {
  const needle = filters.q.trim().toLowerCase();
  return models
    .filter((model) => {
      const identity = getModelIdentity(model.id, model.provider);
      const price = modelPrice(model, filters.price, "input");
      const haystack = [
        model.id,
        identity.displayName,
        identity.author,
        model.description,
        ...model.offers.map((offer) => offer.pool_name),
      ]
        .join(" ")
        .toLowerCase();
      return (
        (!needle || haystack.includes(needle)) &&
        (!filters.authors.length || filters.authors.includes(identity.author)) &&
        (!filters.pools.length || model.offers.some((offer) => filters.pools.includes(offer.pool_id))) &&
        filters.capabilities.every((capability) => model.capabilities[capability] === true) &&
        (!filters.input.length || filters.input.some((modality) => model.input_modalities.includes(modality))) &&
        (filters.context <= 0 || (model.context_length != null && model.context_length >= filters.context)) &&
        (filters.max_price < 0 || (price != null && price <= filters.max_price)) &&
        (filters.availability !== "available" || model.available_to_me) &&
        (filters.mode === "all" || model.mode === filters.mode)
      );
    })
    .sort((left, right) => {
      if (filters.sort === "context") return (right.context_length ?? -1) - (left.context_length ?? -1);
      if (filters.sort.endsWith("_price")) {
        const field = filters.sort === "input_price" ? "input" : "output";
        const a = modelPrice(left, filters.price, field) ?? Number.POSITIVE_INFINITY;
        const b = modelPrice(right, filters.price, field) ?? Number.POSITIVE_INFINITY;
        if (a !== b) return a - b;
      }
      return getModelIdentity(left.id, left.provider).displayName.localeCompare(
        getModelIdentity(right.id, right.provider).displayName,
      );
    });
}

export function catalogDescription(model: CatalogModel): string {
  if (model.description) return model.description;
  const facts = [
    model.input_modalities.length ? `${model.input_modalities.join(", ")} input` : null,
    model.output_modalities.length ? `${model.output_modalities.join(", ")} output` : null,
    ...capabilityOptions
      .filter(({ id }) => model.capabilities[id] === true)
      .slice(0, 3)
      .map(({ label }) => label),
  ].filter(Boolean);
  const identity = getModelIdentity(model.id, model.provider);
  return facts.length
    ? `${identity.displayName} by ${identity.author}. ${facts.join(" · ")}. Explore resource options, pricing and API integration.`
    : "Explore resource options, pricing and API integration. Additional model specifications have not been published.";
}
