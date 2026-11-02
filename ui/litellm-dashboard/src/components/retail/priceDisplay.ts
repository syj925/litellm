export interface TokenPriceDisplayRow {
  conditions: string[];
  rates: Record<string, string>;
  baseline?: boolean;
}

export interface ExpressionPriceDisplay {
  status: "complete" | "unsupported";
  token_rates: TokenPriceDisplayRow[];
  fees: {
    conditions: string[];
    label: string;
    amount: string;
    usage_key: string | null;
    unit: string;
  }[];
  modifiers: {
    conditions: string[];
    multiplier: string;
    applies_to: string;
  }[];
  notes: string[];
}

export const tokenPriceLabels: Readonly<Record<string, string>> = {
  p: "Input",
  len: "Full input context",
  c: "Output",
  cr: "Cache read",
  cc: "Cache write",
  cc5m: "Cache write · 5m",
  cc30m: "Cache write · 30m",
  cc1h: "Cache write · 1h",
  cc12h: "Cache write · 12h",
  img: "Image input",
  img_cr: "Cached image input",
  img_o: "Image output",
  ai: "Audio input",
  ai_cr: "Cached audio input",
  ao: "Audio output",
  reasoning: "Reasoning output",
};

export function baselineTokenPrice(display: ExpressionPriceDisplay | null | undefined): TokenPriceDisplayRow | null {
  return display?.status === "complete" ? display.token_rates.find((row) => row.baseline) ?? null : null;
}

export function hasAdditionalPriceConditions(display: ExpressionPriceDisplay | null | undefined): boolean {
  if (!display || display.status !== "complete") return false;
  return display.token_rates.length > 1 || display.modifiers.length > 0 || display.fees.length > 0;
}

export function tokenPriceColumns(rows: TokenPriceDisplayRow[]): string[] {
  const available = new Set(rows.flatMap((row) => Object.keys(row.rates)));
  return [
    ...Object.keys(tokenPriceLabels).filter((key) => available.has(key)),
    ...[...available].filter((key) => !(key in tokenPriceLabels)),
  ];
}

export function tokenPriceLabel(key: string): string {
  if (key.startsWith("cache_write_")) return `Cache write · ${cacheDurationLabel(key.slice("cache_write_".length))}`;
  return tokenPriceLabels[key] ?? key.replaceAll("_", " ");
}

export function priceConditions(conditions: string[]): string {
  return conditions.join(" · ") || "All requests";
}

export function feeUnitLabel(unit: string): string {
  const labels: Readonly<Record<string, string>> = {
    request: "per request",
    query: "per query",
    image: "per image",
    second: "per second",
    grounded_request: "per grounded request",
  };
  return labels[unit] ?? `per ${unit.replaceAll("_", " ")}`;
}
import { cacheDurationLabel } from "./cachePricing";
