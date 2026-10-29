import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";

export interface PricingPlan {
  id: string;
  name: string;
  multiplier: string;
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

export function usdPerTokenToPerMillion(value: string): string | null {
  const matched = /^\+?(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d{1,3}))?$/.exec(value);
  if (!matched) return null;
  const [, whole, fraction = "", exponent = "0"] = matched;
  const digits = whole + fraction;
  const point = whole.length + 6 + parseInt(exponent, 10);
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
  const priceRows = [
    { key: "input_cost_per_token", label: "Input base price / 1M" },
    { key: "output_cost_per_token", label: "Output base price / 1M" },
    { key: "cache_read_input_token_cost", label: "Cache read base price / 1M" },
    { key: "cache_creation_input_token_cost", label: "Cache write base price / 1M" },
  ].flatMap(({ key, label }) => {
    const value = snapshot[key];
    const scaled = typeof value === "string" ? usdPerTokenToPerMillion(value) : null;
    return scaled === null ? [] : [{ label, value: `${formatMoney(scaled)} USD` }];
  });
  const planRows = [
    { key: "plan_id", label: "Pricing plan" },
    { key: "multiplier", label: "Plan multiplier" },
    { key: "price_version", label: "Price version" },
  ].flatMap(({ key, label }) => {
    const value = snapshot[key];
    if (typeof value !== "string" && !(typeof value === "number" && Number.isSafeInteger(value))) return [];
    return [{ label, value: key === "multiplier" ? `${value}×` : String(value) }];
  });
  return [
    { title: "Token usage", rows: usageRows },
    { title: "Pricing snapshot", rows: [...planRows, ...priceRows] },
  ].filter(({ rows }) => rows.length > 0);
}

export const retailKeys = {
  wallet: (userId: string | null) => ["retail", "wallet", userId] as const,
  ledger: (userId: string | null, kind: string, page: number) => ["retail", "ledger", userId, kind, page] as const,
  wallets: (query: string, page: number) => ["retail", "wallets", query, page] as const,
  plans: ["retail", "plans"] as const,
  config: ["retail", "config"] as const,
};
