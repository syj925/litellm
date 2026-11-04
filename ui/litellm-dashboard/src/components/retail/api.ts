import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";
import { ApiError, createApiClient, type HttpMethod, type RequestOptions } from "@/lib/http/client";
import { cacheDurationLabel, cacheTierEntries } from "./cachePricing";

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

export type ModelDiscountSource =
  | "customer_model"
  | "plan_model"
  | "standard"
  | "legacy_customer"
  | "legacy_plan"
  | "customer_pool_model"
  | "plan_pool_model"
  | "customer_pool_default"
  | "plan_pool_default"
  | "pool_default";

export interface ModelDiscount {
  model: string;
  discount: string;
  discount_source: ModelDiscountSource;
  customer_discount?: string | null;
  plan_discount: string | null;
  pool_id?: string;
  pool_name?: string;
  customer_pool_discount?: string | null;
  plan_pool_discount?: string | null;
  pool_default_discount?: string;
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
    customer_pool_model: "Customer pool-model override",
    plan_pool_model: "Plan pool-model discount",
    customer_pool_default: "Customer pool default",
    plan_pool_default: "Plan pool default",
    pool_default: "Resource pool default",
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
  details: Record<string, unknown> & { expression_pricing?: ExpressionPricingReceipt };
}

export interface ExpressionPricingReceipt {
  version: "expression-v1";
  expression_hash: string;
  expression: string;
  timezone: string;
  started_at: string;
  tiers: string[];
  variables: Record<string, string>;
  usage: Record<string, string>;
  undiscounted_amount: string;
  discount: string;
  amount: string;
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
  key_rotation_enabled?: boolean;
  operation_audit_enabled?: boolean;
  organization_management_enabled?: boolean;
  report_export_enabled?: boolean;
  object_metadata_enabled?: boolean;
}

export class RetailRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "RetailRequestError";
  }
}

function responseMessage(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  if ("detail" in body && typeof body.detail === "string") return body.detail;
  if ("detail" in body && Array.isArray(body.detail)) {
    const messages = body.detail.flatMap((detail: unknown) => {
      const entry = detailRecord(detail);
      return typeof entry.msg === "string" ? [entry.msg] : [];
    });
    if (messages.length) return messages.slice(0, 4).join(" ");
  }
  if ("message" in body && typeof body.message === "string") return body.message;
  if ("error" in body && typeof body.error === "object" && body.error !== null) {
    if ("message" in body.error && typeof body.error.message === "string") return body.error.message;
  }
  return null;
}

const retailClient = createApiClient({
  getBaseUrl: () => `${getProxyBaseUrl()}/retail`,
  getAuthHeaderName: () => getGlobalLitellmHeaderName(),
});

export async function retailRequest<T>(path: string, token: string | null, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body) headers.set("Content-Type", "application/json");
  const request: RequestOptions = {
    accessToken: token,
    rawBody: options.body ?? undefined,
    headers: Object.fromEntries(headers.entries()),
    signal: options.signal ?? undefined,
    credentials: "same-origin",
    cache: "no-store",
    redirect: options.redirect,
  };
  try {
    return await retailClient.request<T>((options.method ?? "GET") as HttpMethod, path, request);
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    throw new RetailRequestError(
      responseMessage(error.body) ?? `The request could not be completed (${error.status}).`, error.status,
    );
  }
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
  let shifted = `${digits.slice(0, point)}.${digits.slice(point)}`;
  if (point <= 0) shifted = `0.${"0".repeat(-point)}${digits}`;
  else if (point >= digits.length) shifted = digits.padEnd(point, "0");
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

function receiptCacheWritePrices(snapshot: Record<string, unknown>, observed: readonly { ttl: string }[]) {
  const saved = detailRecord(snapshot.cache_write_tiers);
  const defaultPrice = snapshot.version === "token-v3" ? snapshot.cache_creation_input_token_cost : null;
  const defaults =
    typeof defaultPrice === "string" ? Object.fromEntries(observed.map(({ ttl }) => [ttl, defaultPrice])) : {};
  return { saved, effective: { ...defaults, ...saved } };
}

export function ledgerReceiptSections(details: Record<string, unknown>): ReceiptSection[] {
  const usage = detailRecord(details.usage);
  const snapshot = detailRecord(details.price_snapshot);
  const expression = detailRecord(details.expression_pricing);
  const hasExpression = expression.version === "expression-v1";
  const discount = typeof snapshot.discount === "string" ? snapshot.discount : snapshot.multiplier;
  const hasReference = snapshot.source === "catalog" || snapshot.source === "manual";
  const usageRows = [
    { key: "input_tokens", label: "Input tokens (uncached)" },
    { key: "output_tokens", label: "Output tokens" },
    { key: "cache_read_tokens", label: "Cache read tokens" },
    { key: "cache_write_tokens", label: "Cache write tokens (total)" },
    { key: "total_tokens", label: "Total tokens" },
  ].flatMap(({ key, label }) => {
    const value = usage[key];
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
      ? [{ label, value: String(value) }]
      : [];
  });
  const cacheWrites = cacheTierEntries(detailRecord(usage.cache_write_ttl_tokens)).flatMap(([ttl, count]) =>
    typeof count === "number" && Number.isSafeInteger(count) && count >= 0 ? [{ ttl, count }] : [],
  );
  const totalWrites = usage.cache_write_tokens;
  const unclassified = usage.cache_write_unclassified_tokens;
  const knownWrites = cacheWrites.reduce((sum, item) => sum + item.count, 0);
  let unclassifiedWrites: number | null = null;
  if (typeof unclassified === "number" && Number.isSafeInteger(unclassified) && unclassified >= 0)
    unclassifiedWrites = unclassified;
  else if (typeof totalWrites === "number" && Number.isSafeInteger(totalWrites) && totalWrites >= knownWrites)
    unclassifiedWrites = totalWrites - knownWrites;
  const durationUsageRows = cacheWrites.map(({ ttl, count }) => ({
    label: `Cache write tokens · ${cacheDurationLabel(ttl)}`,
    value: String(count),
  }));
  if (unclassifiedWrites !== null && (unclassifiedWrites > 0 || cacheWrites.length === 0)) {
    durationUsageRows.push({ label: "Cache write tokens · unspecified duration", value: String(unclassifiedWrites) });
  }
  const prices = [
    { key: "input_cost_per_token", label: "Input base price / 1M" },
    { key: "output_cost_per_token", label: "Output base price / 1M" },
    { key: "cache_read_input_token_cost", label: "Cache read base price / 1M" },
    { key: "cache_creation_input_token_cost", label: "Cache write default base price / 1M" },
  ];
  const priceRows = prices.flatMap(({ key, label }) => {
    if (hasExpression) return [];
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
  const durationPrices = receiptCacheWritePrices(snapshot, cacheWrites);
  const durationPriceRows = cacheTierEntries(durationPrices.effective).flatMap(([ttl, price]) => {
    if (hasExpression) return [];
    const scaled = typeof price === "string" ? usdPerTokenToPerMillion(price) : null;
    const effective = scaled !== null && typeof discount === "string" ? multiplyDecimals(scaled, discount) : null;
    const duration = `${cacheDurationLabel(ttl)}${Object.hasOwn(durationPrices.saved, ttl) ? "" : " (default)"}`;
    return scaled === null
      ? []
      : [
          {
            label: `Cache write · ${duration} ${hasReference ? "reference" : "base"} price / 1M`,
            value: `${formatMoney(scaled)} USD`,
          },
          ...(effective === null
            ? []
            : [
                {
                  label: `Cache write · ${duration} customer price / 1M`,
                  value: `${formatMoney(effective)} USD`,
                },
              ]),
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
  const isKnownDiscountSource =
    typeof snapshot.discount_source === "string" &&
    [
      "customer_model",
      "plan_model",
      "standard",
      "legacy_customer",
      "legacy_plan",
      "customer_pool_model",
      "plan_pool_model",
      "customer_pool_default",
      "plan_pool_default",
      "pool_default",
    ].includes(snapshot.discount_source);
  const sourceRows = [];
  if (isKnownDiscountSource)
    sourceRows.push({
      label: "Discount source",
      value: modelDiscountSourceLabel(snapshot.discount_source as ModelDiscountSource),
    });
  else if (snapshot.discount_source === "customer" || snapshot.discount_source === "plan")
    sourceRows.push({
      label: "Discount source",
      value: snapshot.discount_source === "customer" ? "Customer override" : "Shared plan",
    });
  const modelRows =
    typeof snapshot.discount_model === "string" ? [{ label: "Discount model", value: snapshot.discount_model }] : [];
  const referenceRows =
    typeof snapshot.source_label === "string" ? [{ label: "Reference source", value: snapshot.source_label }] : [];
  const poolRows =
    typeof snapshot.pool_id === "string"
      ? [
          {
            label: "Resource pool",
            value:
              typeof snapshot.pool_name === "string" ? `${snapshot.pool_name} (${snapshot.pool_id})` : snapshot.pool_id,
          },
        ]
      : [];
  const charged = detailRecord(details.cost_breakdown);
  const chargedRows = [
    { key: "input", label: "Uncached input charge" },
    { key: "output", label: "Output charge" },
    { key: "cache_read", label: "Cache read charge" },
    { key: "cache_write_unclassified", label: "Cache write charge · unspecified duration" },
  ].flatMap(({ key, label }) => {
    const amount = charged[key];
    return typeof amount === "string" && /^\d+(?:\.\d+)?$/.test(amount)
      ? [{ label, value: `${formatMoney(amount)} USD` }]
      : [];
  });
  const chargedTierRows = cacheTierEntries(detailRecord(charged.cache_write_tiers)).flatMap(([ttl, amount]) =>
    typeof amount === "string" && /^\d+(?:\.\d+)?$/.test(amount)
      ? [{ label: `Cache write charge · ${cacheDurationLabel(ttl)}`, value: `${formatMoney(amount)} USD` }]
      : [],
  );
  const rounding = charged.rounding_adjustment;
  const roundingRows =
    typeof rounding === "string" && /^-?\d+(?:\.\d+)?$/.test(rounding)
      ? [{ label: "Final rounding adjustment", value: `${formatMoney(rounding)} USD` }]
      : [];
  const expressionRows = hasExpression
    ? [
        ...[
          { key: "expression", label: "Pricing expression" },
          { key: "expression_hash", label: "Expression hash" },
          { key: "timezone", label: "Rule timezone" },
          { key: "started_at", label: "Request start for pricing" },
        ].flatMap(({ key, label }) =>
          typeof expression[key] === "string" ? [{ label, value: String(expression[key]) }] : [],
        ),
        ...(Array.isArray(expression.tiers) && expression.tiers.every((tier) => typeof tier === "string")
          ? [{ label: "Applied pricing branches", value: expression.tiers.join(", ") || "No named branch" }]
          : []),
        ...[
          { key: "undiscounted_amount", label: "Expression charge before discount" },
          { key: "amount", label: "Expression settled charge" },
        ].flatMap(({ key, label }) =>
          typeof expression[key] === "string" && /^\d+(?:\.\d+)?$/.test(String(expression[key]))
            ? [{ label, value: `${formatMoney(String(expression[key]))} USD` }]
            : [],
        ),
        ...(typeof expression.discount === "string"
          ? [{ label: "Expression customer discount", value: discountDescription(expression.discount) }]
          : []),
      ]
    : [];
  const expressionVariables = hasExpression
    ? [
        ...Object.entries(detailRecord(expression.variables)).flatMap(([key, amount]) =>
          typeof amount === "string" && /^-?\d+(?:\.\d+)?$/.test(amount)
            ? [{ label: `Expression variable · ${key}`, value: amount }]
            : [],
        ),
        ...Object.entries(detailRecord(expression.usage)).flatMap(([key, amount]) =>
          typeof amount === "string" && /^\d+(?:\.\d+)?$/.test(amount)
            ? [{ label: `Expression usage · ${key}`, value: amount }]
            : [],
        ),
      ]
    : [];
  return [
    { title: "Token usage", rows: [...usageRows, ...durationUsageRows] },
    { title: "Charge breakdown", rows: [...chargedRows, ...chargedTierRows, ...roundingRows] },
    { title: "Dynamic pricing receipt", rows: expressionRows },
    { title: "Evaluated pricing usage", rows: expressionVariables },
    {
      title: "Pricing snapshot",
      rows: [
        ...planRows,
        ...poolRows,
        ...discountRows,
        ...modelRows,
        ...sourceRows,
        ...referenceRows,
        ...priceRows,
        ...durationPriceRows,
      ],
    },
  ].filter(({ rows }) => rows.length > 0);
}

export function ledgerEntriesCsv(entries: LedgerEntry[]): string {
  const baseFields = ["Transaction ID", "Date", "Type", "Model", "Amount (USD)", "Balance after (USD)", "Request ID"];
  const receipts = entries.map((entry) => ledgerReceiptSections(entry.details).flatMap((section) => section.rows));
  const receiptFields = [...new Set(receipts.flatMap((rows) => rows.map(({ label }) => label)))];
  const escape = (value: string) => {
    const safe =
      /^[=+@\t\r]/.test(value) || (/^-/.test(value) && !/^-\d+(?:\.\d+)?$/.test(value)) ? `'${value}` : value;
    return `"${safe.replaceAll('"', '""')}"`;
  };
  return [
    [...baseFields, ...receiptFields],
    ...entries.map((entry, index) => {
      const receipt = new Map(receipts[index].map(({ label, value }) => [label, value]));
      return [
        entry.id,
        entry.created_at,
        entry.kind,
        entry.model ?? "",
        entry.amount,
        entry.balance_after,
        entry.request_id ?? "",
        ...receiptFields.map((label) => receipt.get(label) ?? ""),
      ];
    }),
  ]
    .map((row) => row.map(escape).join(","))
    .join("\r\n");
}

export const retailKeys = {
  wallet: (userId: string | null) => ["retail", "wallet", userId] as const,
  ledger: (userId: string | null, kind: string, page: number) => ["retail", "ledger", userId, kind, page] as const,
  wallets: (query: string, page: number) => ["retail", "wallets", query, page] as const,
  plans: ["retail", "plans"] as const,
  config: ["retail", "config"] as const,
};
