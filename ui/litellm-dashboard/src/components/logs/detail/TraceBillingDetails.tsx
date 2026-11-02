import CopyButton from "@/components/shared/CopyButton";
import { formatMoney } from "@/components/retail/api";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { JsonViewer } from "./JsonViewer";
import { TraceReceiptSummary } from "./TraceReceiptSummary";
import { traceScalar, traceTimestamp, type RequestTraceBilling } from "./requestTrace";

const recordedAmount = (value: string | null) => (value == null ? "Not recorded" : formatMoney(value));

export function TraceBillingDetails({ billing }: { billing: RequestTraceBilling | null }) {
  if (!billing)
    return (
      <p className="text-sm text-muted-foreground">
        No retail reservation or ledger association was recorded for this call.
      </p>
    );
  const estimatedCost = billing.provider_cost_source === "gateway_cost_calculation";
  return (
    <div className="space-y-4">
      <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-muted-foreground">Retail request ID</dt>
          <dd className="mt-1 flex min-w-0 items-center gap-1">
            <span className="break-all font-mono text-xs">{billing.retail_request_id}</span>
            <CopyButton value={billing.retail_request_id} label="Copy retail request ID" />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Settlement state</dt>
          <dd className="mt-1">{billing.status}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Reserved amount (USD)</dt>
          <dd className="mt-1 tabular-nums">{recordedAmount(billing.reserved_amount)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Settled customer charge (USD)</dt>
          <dd className="mt-1 tabular-nums">{recordedAmount(billing.settled_amount)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">
            {estimatedCost ? "Gateway cost estimate (USD)" : "Recorded cost value (USD)"}
          </dt>
          <dd className="mt-1 tabular-nums">{recordedAmount(billing.provider_cost)}</dd>
          {estimatedCost && (
            <dd className="mt-1 text-xs text-muted-foreground">
              Calculated by the gateway; supplier invoice cost is not recorded.
            </dd>
          )}
          {!estimatedCost && billing.provider_cost_source && (
            <dd className="mt-1 text-xs text-muted-foreground">
              Source: {billing.provider_cost_source.replaceAll("_", " ")}
            </dd>
          )}
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Resource pool / routing version</dt>
          <dd className="mt-1">
            {traceScalar(billing.resource?.pool_name ?? billing.resource?.pool_id)} ·{" "}
            {traceScalar(billing.resource?.routing_settings_version)}
          </dd>
        </div>
      </dl>
      <TraceReceiptSummary billing={billing} />
      <details className="rounded-lg border">
        <summary className="cursor-pointer px-3 py-2 text-sm font-medium">Technical billing details</summary>
        {(
          [
            ["Recorded usage", billing.usage],
            ["Frozen pricing snapshot", billing.price_snapshot],
            ["Recorded charge breakdown", billing.cost_breakdown],
          ] as const
        ).map(([label, value]) => (
          <details key={label} className="rounded-lg border">
            <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
              {label}
              {value == null && <span className="ml-2 text-xs font-normal text-muted-foreground">Not recorded</span>}
            </summary>
            {value != null && <JsonViewer data={value} mode="formatted" />}
          </details>
        ))}
      </details>
      {billing.ledger.length > 0 ? (
        <Table aria-label="Associated retail ledger entries">
          <TableHeader>
            <TableRow>
              <TableHead>Entry ID</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Recorded at (UTC)</TableHead>
              <TableHead className="text-right">Amount (USD)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {billing.ledger.map((entry) => (
              <TableRow key={entry.id}>
                <TableCell className="break-all font-mono text-xs">{entry.id}</TableCell>
                <TableCell>{entry.kind}</TableCell>
                <TableCell className="text-xs">{traceTimestamp(entry.created_at)}</TableCell>
                <TableCell className="text-right tabular-nums">{formatMoney(entry.amount)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : (
        <p className="text-xs text-muted-foreground">No ledger entries were recorded for this reservation.</p>
      )}
    </div>
  );
}
