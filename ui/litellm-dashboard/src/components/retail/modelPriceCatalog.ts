export interface SalesPrice {
  model: string;
  input_per_million: string;
  output_per_million: string;
  cache_read_per_million: string;
  cache_write_per_million: string;
  cache_write_tiers?: Record<string, string>;
  cache_write_price_published?: boolean | null;
}

export interface CatalogPrice extends SalesPrice {
  cache_write_price_published: boolean | null;
  catalog_model: string | null;
  source_label: string;
  source: "manual" | "catalog" | "legacy";
  version: number | null;
  updated_at: string | null;
}

export const pricingStatusLabels = {
  default: "Built-in default",
  custom: "Custom price",
  catalog: "Imported price",
  ambiguous: "Choose reference",
  unavailable: "Needs review",
} as const;

export type PricingStatus = keyof typeof pricingStatusLabels;

export interface PriceConfiguration {
  catalog_model: string;
  source_label: string;
  rates: Record<string, string>;
  base_price: CatalogPrice | null;
}

export interface ModelPriceCatalogEntry {
  model: string;
  providers: string[];
  modes: string[];
  configured: boolean;
  status: PricingStatus;
  price: CatalogPrice | null;
  candidates: CatalogPrice[];
  review_references?: PriceConfiguration[];
  reason: string | null;
}

export function displayedModelPrice(entry: ModelPriceCatalogEntry): CatalogPrice | null {
  if (entry.price) return entry.price;
  if (entry.status !== "unavailable") return null;
  const references = entry.review_references ?? [];
  return references.length === 1 ? references[0].base_price : null;
}

export interface ModelPriceFilters {
  search: string;
  provider: string;
  mode: string;
  status: string;
}

export function filterModelPrices(
  items: ModelPriceCatalogEntry[],
  filters: ModelPriceFilters,
): ModelPriceCatalogEntry[] {
  const terms = filters.search.trim().toLowerCase().split(/\s+/);
  return items.filter((entry) => {
    const text = [
      entry.model,
      ...entry.providers,
      ...entry.modes,
      ...entry.candidates.map((item) => item.catalog_model),
      ...(entry.review_references ?? []).map((item) => item.catalog_model),
    ]
      .join(" ")
      .toLowerCase();
    const matchesSearch = terms.every((term) => text.includes(term));
    const matchesProvider = !filters.provider || entry.providers.includes(filters.provider);
    const matchesMode = !filters.mode || entry.modes.includes(filters.mode);
    const matchesStatus = !filters.status || entry.status === filters.status;
    return [matchesSearch, matchesProvider, matchesMode, matchesStatus].every(Boolean);
  });
}

export function pricingPage(items: ModelPriceCatalogEntry[], pageIndex: number, pageSize: number) {
  const size = Math.max(1, Math.min(100, Math.floor(pageSize)));
  const pageCount = Math.max(1, Math.ceil(items.length / size));
  const index = Math.max(0, Math.min(Math.floor(pageIndex), pageCount - 1));
  const start = index * size;
  return { items: items.slice(start, start + size), index, pageCount, start, size };
}
