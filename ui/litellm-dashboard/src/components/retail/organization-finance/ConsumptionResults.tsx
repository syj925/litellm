"use client";

import CopyButton from "@/components/shared/CopyButton";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatMoney } from "../api";
import { ManagementPagination, managementTimestamp } from "../management/ManagementShared";
import { consumptionLabel } from "./financeInput";
import type { OrganizationConsumption } from "./types";

export default function ConsumptionResults({
  report,
  page,
  loading,
  onPage,
}: {
  report: OrganizationConsumption;
  page: number;
  loading: boolean;
  onPage: (page: number) => void;
}) {
  const { summary } = report;
  const counts = [
    ["Accepted", summary.accepted_requests],
    ["Settled", summary.settled_requests],
    ["Released", summary.released_requests],
    ["Pending", summary.pending_requests],
    ["Unknown", summary.unknown_requests],
    ["Conflict", summary.conflict_requests],
  ] as const;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b pb-4">
        <div className="space-y-1">
          <h2 className="text-lg font-semibold">
            {report.organization_name} {!report.organization_enabled && <Badge variant="outline">Disabled</Badge>}
          </h2>
          <p className="text-sm text-muted-foreground">
            Accepted {managementTimestamp(report.start)} – {managementTimestamp(report.end)} (Beijing)
          </p>
          <p className="text-xs text-muted-foreground">
            Live preview as of {managementTimestamp(report.snapshot_at)} (Beijing)
            {report.partial ? " · Includes an open day" : ""}
          </p>
        </div>
        <dl className="text-right">
          <dt className="text-sm text-muted-foreground">Finalized retail charges (USD)</dt>
          <dd className="text-2xl font-semibold tabular-nums">{formatMoney(summary.settled_usd)}</dd>
        </dl>
      </div>
      <dl className="flex flex-wrap gap-x-8 gap-y-3">
        {counts.map(([label, count]) => (
          <div key={label}>
            <dt className="text-sm text-muted-foreground">{label}</dt>
            <dd className="font-medium tabular-nums">{count.toLocaleString()}</dd>
          </div>
        ))}
      </dl>
      <details className="text-sm text-muted-foreground">
        <summary className="cursor-pointer">What this preview includes</summary>
        <p className="mt-2 max-w-prose">
          Requests belong to the organization captured when they were accepted. Late settlements update that original
          window. Pending, unknown, and conflicting outcomes are separate from finalized charges. Released requests have
          no finalized charge. This live preview can change between pages; it is not a frozen statement.
        </p>
      </details>
      <section className="overflow-hidden rounded-xl border" aria-label="Organization requests">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Request / account</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Accepted (Beijing)</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead className="text-right">Final charge (USD)</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.items.map((item) => (
                <TableRow key={item.request_id}>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <span className="max-w-56 truncate font-mono text-xs" title={item.request_id}>
                        {item.request_id}
                      </span>
                      <CopyButton value={item.request_id} label="Copy request ID" />
                    </div>
                    <p className="mt-1 max-w-56 truncate text-xs text-muted-foreground" title={item.user_id}>
                      {item.user_id}
                    </p>
                  </TableCell>
                  <TableCell className="max-w-56 break-words">{item.model}</TableCell>
                  <TableCell className="whitespace-nowrap text-sm">{managementTimestamp(item.captured_at)}</TableCell>
                  <TableCell>
                    <Badge variant={item.state === "settled" ? "secondary" : "outline"}>
                      {consumptionLabel(item.state)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {item.state === "settled" && item.settled_usd !== null
                      ? formatMoney(item.settled_usd)
                      : "Not finalized"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        {report.items.length === 0 && (
          <p role="status" className="p-5 text-sm text-muted-foreground">
            No requests were accepted for this organization in this window.
          </p>
        )}
        <ManagementPagination page={page} total={report.total} loading={loading} onChange={onPage} />
      </section>
    </div>
  );
}
