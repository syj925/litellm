export interface CatalogTokenPrice {
  input: string | null;
  output: string | null;
  cache_read: string | null;
  cache_write: string | null;
}

export interface CatalogPricing {
  currency: "USD";
  unit: "million_tokens";
  reference: CatalogTokenPrice | null;
  customer: CatalogTokenPrice | null;
  discount: string | null;
  source_label: string | null;
  version: number | null;
  updated_at: string | null;
}

export interface CatalogOffer {
  pool_id: string;
  pool_name: string;
  source_kind: string;
  available_to_me: boolean;
  pricing: CatalogPricing;
}

export interface CatalogCapabilities {
  vision: boolean | null;
  tools: boolean | null;
  reasoning: boolean | null;
  prompt_caching: boolean | null;
  structured_output: boolean | null;
  audio_input: boolean | null;
  audio_output: boolean | null;
  pdf_input: boolean | null;
}

export interface CatalogModel {
  id: string;
  name: string;
  provider: string | null;
  description: string | null;
  context_length: number | null;
  max_output_tokens: number | null;
  mode: string | null;
  input_modalities: string[];
  output_modalities: string[];
  capabilities: CatalogCapabilities;
  metadata_source: "reference_catalog" | "deployment" | null;
  available_to_me: boolean;
  offers: CatalogOffer[];
  reference_pricing?: CatalogTokenPrice | null;
}

export interface CatalogResponse {
  items: CatalogModel[];
  metadata_note: string;
}

export const catalogDetailTabs = ["overview", "resources", "pricing", "api"] as const;
export type CatalogDetailTab = (typeof catalogDetailTabs)[number];
export type CatalogPriceBasis = "reference" | "customer";

export function formatCatalogPrice(value: string | null | undefined): string {
  if (value == null || value.trim() === "") return "Not listed";
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0
    ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 6 }).format(amount)
    : "Not listed";
}

export function modelPrice(
  model: CatalogModel,
  basis: CatalogPriceBasis,
  field: keyof CatalogTokenPrice,
): number | null {
  const values = model.offers.flatMap((offer) => {
    if (basis === "customer" && !offer.available_to_me) return [];
    const value = offer.pricing[basis]?.[field];
    if (value == null || value.trim() === "") return [];
    const amount = Number(value);
    return Number.isFinite(amount) && amount >= 0 ? [amount] : [];
  });
  if (values.length) return Math.min(...values);
  const reference = basis === "reference" ? model.reference_pricing?.[field] : null;
  if (reference == null || reference.trim() === "") return null;
  const amount = Number(reference);
  return Number.isFinite(amount) && amount >= 0 ? amount : null;
}

export function discountLabel(discount: string | null): string | null {
  if (discount == null) return null;
  const factor = Number(discount);
  if (!Number.isFinite(factor) || factor < 0) return null;
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(factor * 100)}% of reference price`;
}
