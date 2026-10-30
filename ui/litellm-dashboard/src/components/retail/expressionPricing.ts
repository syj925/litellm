import type { SalesPrice } from "./modelPriceCatalog";

export interface ExpressionPolicy {
  version: "expression-v1";
  expression: string;
  timezone: string;
  usage_bounds: Record<string, string>;
}

export interface ExpressionPreview {
  amount: string;
  undiscounted_amount: string;
  discount: string;
  tiers: string[];
  variables: Record<string, string>;
  usage?: Record<string, string>;
  started_at: string;
  timezone: string;
}

const usableRate = (value: string) => (/^\d+(?:\.\d+)?$/.test(value) ? value : "0");
const ttlVariables: Record<string, string> = { "300": "cc5m", "1800": "cc30m", "3600": "cc1h", "43200": "cc12h" };

export function fixedRatesExpression(price: SalesPrice): string {
  const durationTerms = Object.entries(price.cache_write_tiers ?? {}).map(
    ([ttl, rate]) => `${ttlVariables[ttl] ?? `cache_write('${String(Number(ttl))}')`} * ${usableRate(rate)}`,
  );
  return [
    `p * ${usableRate(price.input_per_million)}`,
    `c * ${usableRate(price.output_per_million)}`,
    `cr * ${usableRate(price.cache_read_per_million)}`,
    `cc * ${usableRate(price.cache_write_per_million)}`,
    ...durationTerms,
  ].join(" + ");
}

export function policyFromRates(price: SalesPrice): ExpressionPolicy {
  return {
    version: "expression-v1",
    expression: fixedRatesExpression(price),
    timezone: "Asia/Shanghai",
    usage_bounds: {},
  };
}

export const expressionTemplates = [
  {
    id: "context",
    label: "Context tiers",
    expression:
      "len > 200000 ? tier('long-context', p * 4 + c * 16 + cr * 0.4 + cc * 5) : tier('standard-context', p * 2 + c * 8 + cr * 0.2 + cc * 2.5)",
  },
  {
    id: "cache",
    label: "Cache durations",
    expression: "p * 2 + c * 8 + cr * 0.2 + cc * 2.5 + cc5m * 2.5 + cc30m * 3 + cc1h * 4 + cc12h * 6",
  },
  {
    id: "time",
    label: "Peak / weekend",
    expression:
      "weekday() == 0 || weekday() == 6 ? tier('weekend', (p * 2 + c * 8) * 0.8) : (hour() >= 9 && hour() < 18 ? tier('weekday-peak', (p * 2 + c * 8) * 1.2) : tier('off-peak', p * 2 + c * 8))",
  },
  {
    id: "multimodal",
    label: "Image / audio tokens",
    expression: "p * 2 + img * 3 + ai * 10 + c * 8 + img_o * 12 + ao * 20 + cr * 0.2 + cc * 2.5",
  },
  {
    id: "service",
    label: "Service tiers",
    expression:
      "param('service_tier') == 'priority' ? tier('priority', (p * 2 + c * 8) * 2) : tier('standard', p * 2 + c * 8)",
  },
  { id: "images", label: "Per-image fee", expression: "fixed(u('images') * 0.04)", usage_bounds: { images: "4" } },
] satisfies { id: string; label: string; expression: string; usage_bounds?: Record<string, string> }[];

export function parseUsageBounds(value: string): { bounds: Record<string, string>; error: string | null } {
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
      return { bounds: {}, error: "Usage bounds must be a JSON object." };
    const entries = Object.entries(parsed);
    if (entries.some(([key, amount]) => !key || typeof amount !== "string" || !/^\d+(?:\.\d+)?$/.test(amount)))
      return {
        bounds: {},
        error: 'Each usage bound needs a name and a non-negative decimal string, for example {"images":"4"}.',
      };
    return { bounds: Object.fromEntries(entries), error: null };
  } catch {
    return { bounds: {}, error: "Enter valid JSON for usage bounds." };
  }
}

export function parsePreviewParameters(value: string): { params: Record<string, unknown>; error: string | null } {
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
      return { params: {}, error: "Synthetic request parameters must be a JSON object." };
    const supported = new Set([
      "quality",
      "size",
      "resolution",
      "duration",
      "reasoning_effort",
      "prompt_cache_retention",
    ]);
    const valid = Object.entries(parsed).every(
      ([key, item]) => supported.has(key) && ["string", "number", "boolean"].includes(typeof item),
    );
    return valid
      ? { params: parsed as Record<string, unknown>, error: null }
      : {
          params: {},
          error:
            "Use scalar quality, size, resolution, duration, reasoning_effort or prompt_cache_retention parameters.",
        };
  } catch {
    return { params: {}, error: "Enter valid JSON for synthetic request parameters." };
  }
}
