import { cacheWritePriceRows } from "../cachePricing";

export interface CatalogTokenPrice {
  input: string | null;
  output: string | null;
  cache_read: string | null;
  cache_write: string | null;
  cache_write_tiers?: Record<string, string>;
}

export function catalogPriceRows(
  price: CatalogTokenPrice | null | undefined,
): { key: string; label: string; amount: string | null }[] {
  const baseRows = [
    { key: "input", label: "Input", amount: price?.input ?? null },
    { key: "output", label: "Output", amount: price?.output ?? null },
    { key: "cache_read", label: "Cache read", amount: price?.cache_read ?? null },
  ];
  return [
    ...baseRows,
    ...cacheWritePriceRows(price?.cache_write, price?.cache_write_tiers),
  ];
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

export interface CatalogMetadataSource {
  source: "models_dev" | "openrouter" | "deployment" | "litellm";
  url: string | null;
  retrieved_at: string | null;
  fields: string[];
  model_id: string | null;
}

export interface BenchmarkRecord {
  name: string;
  score: number;
  metric: string | null;
  source_url: string;
  source: "models_dev" | "openrouter";
  retrieved_at: string;
  version: string | null;
  harness: string | null;
  dataset: string | null;
  variant: string | null;
  evaluated_at: string | null;
  rank: number | null;
}

export interface CatalogModel {
  id: string;
  name: string;
  provider: string | null;
  description: string | null;
  context_length: number | null;
  context_window?: number | null;
  max_output_tokens: number | null;
  mode: string | null;
  input_modalities: string[];
  output_modalities: string[];
  capabilities: CatalogCapabilities;
  metadata_source: "reference_catalog" | "deployment" | null;
  metadata_sources?: CatalogMetadataSource[];
  metadata_conflicts?: string[];
  benchmarks?: BenchmarkRecord[];
  available_to_me: boolean;
  offers: CatalogOffer[];
  reference_pricing?: CatalogTokenPrice | null;
}

export interface CatalogResponse {
  items: CatalogModel[];
  metadata_note: string;
}

export interface ModelPerformanceMetrics {
  requests: number;
  successful_requests: number;
  failed_requests: number;
  unknown_status_requests: number;
  success_rate_pct: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  cache_write_ttl_tokens?: Record<string, number>;
  cache_write_unclassified_tokens?: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
  output_tokens_per_second_p50: number | null;
  ttft_samples: number;
  throughput_samples: number;
}

export interface ModelPerformanceResponse {
  model: string;
  scope: "gateway" | "account";
  window: { start: string; end: string; hours: number; bucket_minutes: number };
  summary: ModelPerformanceMetrics;
  trend: (ModelPerformanceMetrics & { time: string })[];
  notes?: string[];
}

export const catalogDetailTabs = ["overview", "resources", "performance", "benchmarks", "pricing", "api"] as const;
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
  field: Exclude<keyof CatalogTokenPrice, "cache_write_tiers">,
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
