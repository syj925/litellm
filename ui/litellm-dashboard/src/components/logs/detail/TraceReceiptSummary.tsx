import { formatMoney } from "@/components/retail/api";
import { cacheDurationLabel } from "@/components/retail/cachePricing";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { traceTimestamp, type RequestTraceBilling, type TraceValue } from "./requestTrace";

const usageLabels: Readonly<Record<string, string>> = {
  input_tokens: "Uncached input tokens",
  output_tokens: "Output tokens",
  cache_read_tokens: "Cache read tokens",
  cache_write_tokens: "Cache write tokens",
  cache_write_unclassified_tokens: "Other cache write tokens",
  total_tokens: "Total reported tokens",
  images: "Returned images",
  web_search_requests: "Web searches",
  google_maps_grounding_requests: "Maps grounding requests",
  audio_seconds: "Audio seconds",
};

function scalar(value: TraceValue | undefined): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "Not recorded";
}

function numericRows(value: Record<string, TraceValue> | null | undefined): [string, string][] {
  return Object.entries(value ?? {}).flatMap(([key, item]) => {
    const amount = typeof item === "string" || typeof item === "number" ? String(item) : "";
    return /^-?\d+(?:\.\d+)?$/.test(amount) ? [[key, amount] as [string, string]] : [];
  });
}

function ttlRows(usage: RequestTraceBilling["usage"]): [string, string][] {
  const tiers = usage?.cache_write_ttl_tokens;
  if (tiers == null || typeof tiers !== "object" || Array.isArray(tiers)) return [];
  return numericRows(tiers).map(([duration, amount]) => [
    `Cache write tokens · ${cacheDurationLabel(duration)}`,
    amount,
  ]);
}

export function TraceReceiptSummary({ billing }: { billing: RequestTraceBilling }) {
  const pricing = billing.expression_pricing;
  const snapshot = billing.price_snapshot;
  const quantities = [
    ...numericRows(billing.usage).map(([key, amount]) => [usageLabels[key] ?? key.replaceAll("_", " "), amount]),
    ...ttlRows(billing.usage),
    ...numericRows(pricing?.usage)
      .filter(([key]) => billing.usage?.[key] == null)
      .map(([key, amount]) => [usageLabels[key] ?? key.replaceAll("_", " "), amount]),
  ];
  const discount = pricing?.discount ?? snapshot?.discount ?? snapshot?.multiplier;
  const fields = [
    ["Frozen price version", scalar(snapshot?.price_version)],
    ["Pricing format", scalar(snapshot?.version)],
    ["Frozen discount factor", discount == null ? "Not recorded" : `${scalar(discount)} × base price`],
    [
      "Before discount (USD)",
      pricing?.undiscounted_amount == null ? "Not recorded" : formatMoney(pricing.undiscounted_amount),
    ],
    ["After discount (USD)", pricing?.amount ?? billing.settled_amount],
    [
      "Rounding adjustment (USD)",
      pricing?.rounding_adjustment == null ? "Not recorded" : formatMoney(pricing.rounding_adjustment),
    ],
    ["Frozen request start (UTC)", traceTimestamp(pricing?.started_at)],
    ["Rule timezone", pricing?.timezone ?? "Not recorded"],
  ];
  const breakdown = numericRows(billing.cost_breakdown);
  return (
    <div className="space-y-4">
      <section aria-label="Frozen pricing and settlement" className="space-y-3 rounded-lg border p-3">
        <h4 className="text-sm font-medium">Frozen pricing and settlement</h4>
        <dl className="grid gap-3 text-xs sm:grid-cols-2">
          {fields.map(([label, value]) => (
            <div key={label}>
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="mt-1 break-all tabular-nums">
                {label === "After discount (USD)" && value != null ? formatMoney(value) : value ?? "Not recorded"}
              </dd>
            </div>
          ))}
        </dl>
        <div>
          <p className="text-xs text-muted-foreground">Matched price tiers</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {pricing?.tiers?.map((tier) => (
              <Badge key={tier} variant="secondary">
                {tier}
              </Badge>
            ))}
            {pricing?.tiers?.length === 0 && <span className="text-xs">No named price tier</span>}
            {pricing?.tiers == null && <span className="text-xs">Not recorded</span>}
          </div>
        </div>
      </section>
      {quantities.length > 0 ? (
        <Table aria-label="Recorded billable usage">
          <TableHeader>
            <TableRow>
              <TableHead>Usage</TableHead>
              <TableHead className="text-right">Reported quantity</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {quantities.map(([label, amount]) => (
              <TableRow key={label}>
                <TableCell>{label}</TableCell>
                <TableCell className="text-right tabular-nums">{amount}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <p className="text-xs text-muted-foreground">Billable usage was not recorded.</p>
      )}
      {breakdown.length > 0 && (
        <Table aria-label="Recorded monetary breakdown">
          <TableHeader>
            <TableRow>
              <TableHead>Charge component</TableHead>
              <TableHead className="text-right">USD</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {breakdown.map(([label, amount]) => (
              <TableRow key={label}>
                <TableCell>{label.replaceAll("_", " ")}</TableCell>
                <TableCell className="text-right tabular-nums">{formatMoney(amount)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
