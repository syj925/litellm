"use client";

import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatMoney } from "../../api";
import { ManagementPagination, managementTimestamp } from "../../management/ManagementShared";
import { consumptionLabel } from "../financeInput";
import type { DelegatedConsumption } from "./types";

export default function DelegatedResults({
  data,
  loading,
  page,
  onPage,
  exports,
}: {
  data: DelegatedConsumption | undefined;
  loading: boolean;
  page: number;
  onPage: (page: number) => void;
  exports?: ReactNode;
}) {
  const counts = data
    ? ([
        ["Accepted", data.summary.accepted_requests],
        ["Settled", data.summary.settled_requests],
        ["Released", data.summary.released_requests],
        ["Pending", data.summary.pending_requests],
        ["Unknown", data.summary.unknown_requests],
        ["Conflict", data.summary.conflict_requests],
      ] as const)
    : [];
  return (
    <div className="space-y-5">
      {data && (
        <>
          <div className="flex flex-wrap items-start justify-between gap-4 border-b pb-4">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold">{data.organization_name}</h2>
              <p className="text-sm text-muted-foreground">
                Accepted {managementTimestamp(data.start)} – {managementTimestamp(data.end)} (Beijing)
              </p>
              <p className="text-xs text-muted-foreground">
                Live as of {managementTimestamp(data.snapshot_at)} (Beijing)
                {data.partial ? " · Includes an open day" : ""}
              </p>
            </div>
            <dl className="text-right">
              <dt className="text-sm text-muted-foreground">Finalized retail charges (USD)</dt>
              <dd className="text-2xl font-semibold tabular-nums">{formatMoney(data.summary.settled_usd)}</dd>
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
          <p className="max-w-prose text-sm text-muted-foreground">
            Requests stay with the organization recorded when accepted. Later settlements update this live view.
            Pending, unknown, and conflicting outcomes are separate from finalized charges.
          </p>
        </>
      )}
      {exports}
      {data && (
        <>
          <section className="overflow-hidden rounded-xl border" aria-label="Authorized organization usage">
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Sequence</TableHead>
                    <TableHead>Model</TableHead>
                    <TableHead>Accepted (Beijing)</TableHead>
                    <TableHead>Outcome</TableHead>
                    <TableHead className="text-right">Final charge (USD)</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.items.map((item) => (
                    <TableRow key={item.sequence}>
                      <TableCell className="tabular-nums">{item.sequence}</TableCell>
                      <TableCell className="max-w-56 break-words">{item.model}</TableCell>
                      <TableCell className="whitespace-nowrap text-sm">
                        {managementTimestamp(item.captured_at)}
                      </TableCell>
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
            {data.items.length === 0 && (
              <p role="status" className="p-4 text-sm text-muted-foreground">
                No accepted requests in this window.
              </p>
            )}
            <ManagementPagination page={page} total={data.total} loading={loading} onChange={onPage} />
          </section>
        </>
      )}
    </div>
  );
}
