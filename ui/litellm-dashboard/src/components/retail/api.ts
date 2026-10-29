import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";

export interface PricingPlan {
  id: string;
  name: string;
  multiplier: string;
  discount?: string;
}

export interface WalletAccount {
  user_id: string;
  currency: string;
  balance: string;
  reserved: string;
  available: string;
  total_credited: string;
  total_spent: string;
  pending_requests: number;
  pricing_plan: PricingPlan;
  discount?: string;
  discount_override?: string | null;
  discount_source?: "customer" | "plan";
}

export type ModelDiscountSource = "customer_model" | "plan_model" | "standard" | "legacy_customer" | "legacy_plan";

export interface ModelDiscount {
  model: string;
  discount: string;
  discount_source: ModelDiscountSource;
  customer_discount?: string | null;
  plan_discount: string | null;
}

export interface ModelDiscountList {
  user_id?: string;
  plan_id: string;
  items: ModelDiscount[];
}

export function modelDiscountSourceLabel(source: ModelDiscountSource): string {
  const labels = {
    customer_model: "Customer model override",
    plan_model: "Shared plan model discount",
    standard: "Standard reference price",
    legacy_customer: "Legacy account fallback",
    legacy_plan: "Legacy plan fallback",
  };
  return labels[source];
}

export interface LedgerEntry {
  id: string;
  kind: "credit" | "charge" | "release";
  amount: string;
  balance_after: string;
  request_id: string | null;
  model: string | null;
  created_at: string;
  description: string;
  details: Record<string, unknown>;
}

export interface PageResult<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface RetailConfig {
  registration_enabled: boolean;
  currency: string;
}

function responseMessage(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  if ("detail" in body && typeof body.detail === "string") return body.detail;
  if ("message" in body && typeof body.message === "string") return body.message;
  if ("error" in body && typeof body.error === "object" && body.error !== null) {
    if ("message" in body.error && typeof body.error.message === "string") return body.error.message;
  }
  return null;
}

export async function retailRequest<T>(path: string, token: string | null, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  if (token) headers.set(getGlobalLitellmHeaderName(), `Bearer ${token}`);
  const response = await fetch(`${getProxyBaseUrl()}/retail${path}`, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(responseMessage(body) ?? `The request could not be completed (${response.status}).`);
  }
  return body as T;
}

export function formatMoney(value: string): string {
  const matched = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (!matched) return value;
  const [, sign, whole, fraction = ""] = matched;
  const decimals = fraction.replace(/0+$/, "").padEnd(2, "0");
  return `${sign}$${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${decimals}`;
}

export function isPositiveAmount(value: string): boolean {
  return /^\d{1,9}(?:\.\d{1,9})?$/.test(value) && /[1-9]/.test(value);
}

function shiftDecimal(value: string, places: number): string | null {
  const matched = /^\+?(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d{1,3}))?$/.exec(value);
  if (!matched) return null;
  const [, whole, fraction = "", exponent = "0"] = matched;
  const digits = whole + fraction;
  const point = whole.length + places + parseInt(exponent, 10);
  const shifted =
    point <= 0
      ? `0.${"0".repeat(-point)}${digits}`
      : point >= digits.length
        ? digits.padEnd(point, "0")
        : `${digits.slice(0, point)}.${digits.slice(point)}`;
  const [integer, decimals = ""] = shifted.split(".");
  const normalizedInteger = integer.replace(/^0+(?=\d)/, "");
  const normalizedDecimals = decimals.replace(/0+$/, "");
  return normalizedDecimals ? `${normalizedInteger}.${normalizedDecimals}` : normalizedInteger;
}

export function usdPerTokenToPerMillion(value: string): string | null {
  return shiftDecimal(value, 6);
}

function decimalFromInteger(value: bigint, places: number): string {
  const digits = value.toString().padStart(places + 1, "0");
  const whole = digits.slice(0, -places);
  const fraction = digits.slice(-places).replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole;
}

export function percentToDiscount(value: string): string | null {
  const matched = /^(\d{1,3})(?:\.(\d{1,7}))?$/.exec(value);
  if (!matched) return null;
  const units = BigInt(matched[1]) * BigInt(10_000_000) + BigInt((matched[2] ?? "").padEnd(7, "0"));
  return units > BigInt(0) && units <= BigInt(1_000_000_000) ? decimalFromInteger(units, 9) : null;
}

export function discountToPercent(value: string): string {
  return shiftDecimal(value, 2) ?? value;
}

export function discountDescription(value: string): string {
  const percent = discountToPercent(value);
  const normalized = shiftDecimal(value, 9);
  if (!normalized || !/^\d+$/.test(normalized)) return `${percent}% of reference price`;
  const units = BigInt(normalized);
  if (units > BigInt(1_000_000_000)) return `Legacy markup: ${percent}% of reference price`;
  return `${percent}% of reference price (${decimalFromInteger(BigInt(1_000_000_000) - units, 7)}% off)`;
}

export function effectiveDiscount(wallet: WalletAccount): string {
  return wallet.discount ?? wallet.pricing_plan.discount ?? wallet.pricing_plan.multiplier;
}

function multiplyDecimals(left: string, right: string): string | null {
  const a = /^(\d+)(?:\.(\d+))?$/.exec(left);
  const b = /^(\d+)(?:\.(\d+))?$/.exec(right);
  if (!a || !b) return null;
  const fractionA = a[2] ?? "";
  const fractionB = b[2] ?? "";
  const units = BigInt(a[1] + fractionA) * BigInt(b[1] + fractionB);
  const places = fractionA.length + fractionB.length;
  return places === 0 ? units.toString() : decimalFromInteger(units, places);
}

export interface ReceiptSection {
  title: string;
  rows: { label: string; value: string }[];
}

function detailRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function ledgerReceiptSections(details: Record<string, unknown>): ReceiptSection[] {
  const usage = detailRecord(details.usage);
  const snapshot = detailRecord(details.price_snapshot);
  const discount = typeof snapshot.discount === "string" ? snapshot.discount : snapshot.multiplier;
  const hasReference = snapshot.source === "catalog" || snapshot.source === "manual";
  const usageRows = [
    { key: "input_tokens", label: "Input tokens (uncached)" },
    { key: "output_tokens", label: "Output tokens" },
    { key: "cache_read_tokens", label: "Cache read tokens" },
    { key: "cache_write_tokens", label: "Cache write tokens" },
    { key: "total_tokens", label: "Total tokens" },
  ].flatMap(({ key, label }) => {
    const value = usage[key];
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
      ? [{ label, value: String(value) }]
      : [];
  });
  const prices = [
    { key: "input_cost_per_token", label: "Input base price / 1M" },
    { key: "output_cost_per_token", label: "Output base price / 1M" },
    { key: "cache_read_input_token_cost", label: "Cache read base price / 1M" },
    { key: "cache_creation_input_token_cost", label: "Cache write base price / 1M" },
  ];
  const priceRows = prices.flatMap(({ key, label }) => {
    const value = snapshot[key];
    const scaled = typeof value === "string" ? usdPerTokenToPerMillion(value) : null;
    const referenceLabel = hasReference ? label.replace("base price", "reference price") : label;
    const effective = scaled !== null && typeof discount === "string" ? multiplyDecimals(scaled, discount) : null;
    return scaled === null
      ? []
      : [
          { label: referenceLabel, value: `${formatMoney(scaled)} USD` },
          ...(effective === null
            ? []
            : [{ label: label.replace("base price", "customer price"), value: `${formatMoney(effective)} USD` }]),
        ];
  });
  const planRows = [
    { key: "plan_id", label: "Discount plan" },
    { key: "price_version", label: "Price version" },
  ].flatMap(({ key, label }) => {
    const value = snapshot[key];
    if (typeof value !== "string" && !(typeof value === "number" && Number.isSafeInteger(value))) return [];
    return [{ label, value: String(value) }];
  });
  const discountRows =
    typeof discount === "string" ? [{ label: "Customer discount", value: discountDescription(discount) }] : [];
  const sourceRows =
    typeof snapshot.discount_source === "string" &&
    ["customer_model", "plan_model", "standard", "legacy_customer", "legacy_plan"].includes(snapshot.discount_source)
      ? [{ label: "Discount source", value: modelDiscountSourceLabel(snapshot.discount_source as ModelDiscountSource) }]
      : snapshot.discount_source === "customer" || snapshot.discount_source === "plan"
        ? [
            {
              label: "Discount source",
              value: snapshot.discount_source === "customer" ? "Customer override" : "Shared plan",
            },
          ]
        : [];
  const modelRows =
    typeof snapshot.discount_model === "string" ? [{ label: "Discount model", value: snapshot.discount_model }] : [];
  const referenceRows =
    typeof snapshot.source_label === "string" ? [{ label: "Reference source", value: snapshot.source_label }] : [];
  return [
    { title: "Token usage", rows: usageRows },
    {
      title: "Pricing snapshot",
      rows: [...planRows, ...discountRows, ...modelRows, ...sourceRows, ...referenceRows, ...priceRows],
    },
  ].filter(({ rows }) => rows.length > 0);
}

export const retailKeys = {
  wallet: (userId: string | null) => ["retail", "wallet", userId] as const,
  ledger: (userId: string | null, kind: string, page: number) => ["retail", "ledger", userId, kind, page] as const,
  wallets: (query: string, page: number) => ["retail", "wallets", query, page] as const,
  plans: ["retail", "plans"] as const,
  config: ["retail", "config"] as const,
};
