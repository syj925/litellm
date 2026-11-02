import { upstreamSearchAllowanceNote, type ExpressionPolicy } from "./expressionPricing";
import { formatMoney } from "./api";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  feeUnitLabel,
  priceConditions,
  tokenPriceColumns,
  tokenPriceLabel,
  type ExpressionPriceDisplay,
} from "./priceDisplay";

export default function ExpressionPricingRule({
  policy,
  display,
  cacheWritePublished,
}: {
  policy: ExpressionPolicy;
  display?: ExpressionPriceDisplay | null;
  cacheWritePublished?: boolean | null;
}) {
  const complete = display?.status === "complete";
  const rows = complete ? display.token_rates : [];
  const columns = tokenPriceColumns(rows);
  return (
    <section aria-label="Dynamic pricing rule" className="space-y-3 p-4 text-sm">
      {rows.length > 0 && (
        <div className="overflow-hidden rounded-lg border">
          <div className="border-b bg-muted/25 px-4 py-3">
            <h3 className="font-medium">Token prices</h3>
            <p className="mt-1 text-xs text-muted-foreground">USD per 1M tokens, before account discounts</p>
          </div>
          <Table aria-label="Token prices by condition">
            <TableHeader>
              <TableRow>
                <TableHead className="min-w-56">When</TableHead>
                {columns.map((key) => (
                  <TableHead key={key} className="text-right whitespace-normal">
                    {key === "cc" && cacheWritePublished === false
                      ? "Cache write (input-rate fallback)"
                      : tokenPriceLabel(key)}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row, index) => (
                <TableRow key={index}>
                  <TableCell className="max-w-96 whitespace-normal">{priceConditions(row.conditions)}</TableCell>
                  {columns.map((key) => (
                    <TableCell key={key} className="text-right tabular-nums">
                      {row.rates[key] == null ? "Not listed" : formatMoney(row.rates[key])}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {complete && display.fees.length > 0 && (
        <div className="overflow-hidden rounded-lg border">
          <h3 className="border-b bg-muted/25 px-4 py-3 font-medium">Additional charges</h3>
          <Table aria-label="Additional charges">
            <TableHeader>
              <TableRow>
                <TableHead>Usage</TableHead>
                <TableHead>When</TableHead>
                <TableHead className="text-right">Price</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {display.fees.map((fee, index) => (
                <TableRow key={index}>
                  <TableCell>{fee.label}</TableCell>
                  <TableCell className="max-w-80 whitespace-normal text-muted-foreground">
                    {fee.conditions.length
                      ? priceConditions(fee.conditions)
                      : fee.usage_key
                        ? "When used"
                        : "Every request"}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatMoney(fee.amount)}{" "}
                    <span className="text-xs text-muted-foreground">{feeUnitLabel(fee.unit)}</span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {complete && display.modifiers.length > 0 && (
        <div className="rounded-lg border px-4 py-3">
          <h3 className="font-medium">Price adjustments</h3>
          <div className="mt-2 space-y-2">
            {display.modifiers.map((modifier, index) => (
              <p key={index} className="flex flex-wrap justify-between gap-3">
                <span>{priceConditions(modifier.conditions)}</span>
                <span className="font-medium">
                  ×{modifier.multiplier}{" "}
                  <span className="font-normal text-muted-foreground">
                    {modifier.applies_to === "tokens" ? "on token charges" : `on ${modifier.applies_to.toLowerCase()}`}
                  </span>
                </span>
              </p>
            ))}
          </div>
        </div>
      )}
      {!complete && (
        <div className="rounded-lg border bg-muted/20 px-4 py-3">
          <p className="font-medium">Request-dependent price</p>
          <p className="mt-1 leading-6 text-muted-foreground">
            This custom rule cannot be represented as a fixed price table. Review its conditions with the administrator
            before use.
          </p>
        </div>
      )}
      {display?.notes.map((note, index) => (
        <p key={index} className="text-xs leading-6 text-muted-foreground">
          {note}
        </p>
      ))}
      {complete &&
        display.fees.some(
          (fee) => fee.usage_key === "web_search_requests" || fee.usage_key === "google_maps_grounding_requests",
        ) && <p className="text-xs leading-6 text-muted-foreground">{upstreamSearchAllowanceNote}</p>}
      <p className="text-xs text-muted-foreground">
        Your applicable account or resource pool discount applies to the calculated charge.
      </p>
      <details className="rounded-lg border text-xs text-muted-foreground">
        <summary className="cursor-pointer px-4 py-3 font-medium">Technical details</summary>
        <div className="space-y-3 border-t px-4 py-3">
          <p>Timezone: {policy.timezone}</p>
          <pre className="max-h-64 overflow-auto rounded-md bg-muted/40 p-3 font-mono text-xs leading-6 whitespace-pre-wrap break-words">
            {policy.expression}
          </pre>
        </div>
      </details>
    </section>
  );
}
