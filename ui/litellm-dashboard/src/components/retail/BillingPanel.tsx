"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, CircleAlert, Download, FileText, RefreshCw } from "lucide-react";
import { parseAsInteger, parseAsStringLiteral, useQueryStates } from "nuqs";
import { useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import CopyButton from "@/components/shared/CopyButton";
import { StateMessage } from "@/components/shared/StateMessage";
import { LoadingState } from "@/components/shared/LoadingState";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  formatMoney,
  ledgerEntriesCsv,
  ledgerReceiptSections,
  LedgerEntry,
  PageResult,
  retailKeys,
  retailRequest,
} from "./api";

const transactionTypes = ["all", "credit", "charge", "release"] as const;
const typeLabels = {
  all: "All transactions",
  credit: "Credits",
  charge: "Usage charges",
  release: "Released reservations",
};
const entryLabels = { credit: "Credit", charge: "Usage charge", release: "Release" };
const pageSize = 25;

function timestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}

export function LedgerTable({
  entries,
  loading,
  error,
}: {
  entries: LedgerEntry[];
  loading: boolean;
  error: Error | null;
}) {
  const [selected, setSelected] = useState<LedgerEntry | null>(null);

  if (loading) return <LoadingState title="Loading transactions" description="Retrieving your account activity." />;
  if (error)
    return (
      <StateMessage
        role="alert"
        tone="destructive"
        icon={<CircleAlert className="size-5" />}
        title="Unable to load transactions"
        description={error.message}
      />
    );
  if (entries.length === 0)
    return (
      <StateMessage
        role="status"
        icon={<FileText className="size-5" />}
        title="No transactions yet"
        description="Confirmed top-ups and settled API charges will appear here."
      />
    );

  return (
    <>
      <Table>
        <TableHeader className="bg-muted/40">
          <TableRow>
            <TableHead className="pl-5">Date</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Model / Description</TableHead>
            <TableHead className="text-right">Amount (USD)</TableHead>
            <TableHead className="text-right">Balance after</TableHead>
            <TableHead className="pr-5">
              <span className="sr-only">Details</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {entries.map((entry) => (
            <TableRow key={entry.id}>
              <TableCell className="pl-5 text-muted-foreground">{timestamp(entry.created_at)}</TableCell>
              <TableCell>
                <Badge
                  variant="outline"
                  className={entry.kind === "credit" ? "border-success/20 bg-success/5 text-success" : ""}
                >
                  {entryLabels[entry.kind] ?? entry.kind}
                </Badge>
              </TableCell>
              <TableCell className="max-w-72">
                <p className="truncate font-medium">{entry.model || entry.description || entryLabels[entry.kind]}</p>
                {entry.model && entry.description && (
                  <p className="mt-1 truncate text-xs text-muted-foreground">{entry.description}</p>
                )}
              </TableCell>
              <TableCell
                className={`text-right font-medium tabular-nums ${entry.kind === "credit" ? "text-success" : ""}`}
              >
                {entry.kind === "credit" && !entry.amount.startsWith("-") ? "+" : ""}
                {formatMoney(entry.amount)}
              </TableCell>
              <TableCell className="text-right text-muted-foreground tabular-nums">
                {formatMoney(entry.balance_after)}
              </TableCell>
              <TableCell className="pr-5 text-right">
                <Button
                  size="xs"
                  variant="ghost"
                  onClick={() => setSelected(entry)}
                  aria-label={`View transaction ${entry.id}`}
                >
                  Details
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Dialog
        open={selected !== null}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      >
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Transaction details</DialogTitle>
            <DialogDescription>Your account entry and request reference.</DialogDescription>
          </DialogHeader>
          {selected && (
            <div className="flex flex-col gap-5">
              <div className="rounded-lg border bg-muted/25 p-4">
                <p className="text-xs text-muted-foreground">{entryLabels[selected.kind]}</p>
                <p className="mt-1 text-3xl font-semibold tracking-tight tabular-nums">
                  {formatMoney(selected.amount)} <span className="text-sm font-normal text-muted-foreground">USD</span>
                </p>
              </div>
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-4 text-sm">
                <dt className="text-muted-foreground">Recorded at</dt>
                <dd className="text-right">{timestamp(selected.created_at)}</dd>
                <dt className="text-muted-foreground">Balance after</dt>
                <dd className="text-right tabular-nums">{formatMoney(selected.balance_after)}</dd>
                <dt className="text-muted-foreground">Transaction ID</dt>
                <dd className="flex min-w-0 items-center justify-end gap-1">
                  <span className="break-all text-right font-mono text-xs">{selected.id}</span>
                  <CopyButton value={selected.id} label="Copy transaction ID" />
                </dd>
                {selected.model && (
                  <>
                    <dt className="text-muted-foreground">Model</dt>
                    <dd className="break-all text-right">{selected.model}</dd>
                  </>
                )}
                {selected.request_id && (
                  <>
                    <dt className="text-muted-foreground">Request ID</dt>
                    <dd className="flex min-w-0 items-center justify-end gap-1">
                      <span className="break-all text-right font-mono text-xs">{selected.request_id}</span>
                      <CopyButton value={selected.request_id} label="Copy request ID" />
                    </dd>
                  </>
                )}
                {selected.description && (
                  <>
                    <dt className="text-muted-foreground">Description</dt>
                    <dd className="break-words text-right">{selected.description}</dd>
                  </>
                )}
              </dl>
              {ledgerReceiptSections(selected.details).map((section) => (
                <section key={section.title} className="border-t pt-4">
                  <h3 className="mb-4 text-sm font-medium">{section.title}</h3>
                  <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-3 text-sm">
                    {section.rows.map(({ label, value }) => (
                      <div key={label} className="contents">
                        <dt className="text-muted-foreground">{label}</dt>
                        <dd className="break-all text-right tabular-nums">{value}</dd>
                      </div>
                    ))}
                  </dl>
                  {section.title === "Token usage" && (
                    <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
                      Cache write total includes the duration breakdown below it. Unspecified duration means no reliable
                      duration was recorded; it is not inferred from the model name.
                    </p>
                  )}
                  {section.title === "Charge breakdown" && (
                    <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
                      Saved amounts already include your negotiated discount. The total is rounded once; category
                      amounts are not recalculated from today&apos;s prices.
                    </p>
                  )}
                  {section.title === "Pricing snapshot" && (
                    <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
                      These are the reference prices, customer discount, and effective token prices saved for this
                      request. Later price or discount changes do not alter this bill.
                    </p>
                  )}
                  {section.title === "Dynamic pricing receipt" && (
                    <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
                      This saved rule, request start time and selected branches produced the settled charge. The amount
                      includes the saved discount and final currency rounding.
                    </p>
                  )}
                </section>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

export default function BillingPanel() {
  const { accessToken, userId, isAuthorized } = useAuthorized();
  const [filters, setFilters] = useQueryStates({
    page: parseAsInteger.withDefault(1),
    type: parseAsStringLiteral(transactionTypes).withDefault("all"),
  });
  const page = Math.max(1, filters.page);
  const ledger = useQuery({
    queryKey: retailKeys.ledger(userId, filters.type, page),
    queryFn: () =>
      retailRequest<PageResult<LedgerEntry>>(
        `/ledger?limit=${pageSize}&offset=${(page - 1) * pageSize}${filters.type === "all" ? "" : `&kind=${filters.type}`}`,
        accessToken,
      ),
    enabled: isAuthorized && Boolean(accessToken),
  });
  const total = ledger.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const exportPage = () => {
    const blob = new Blob(["\uFEFF", ledgerEntriesCsv(ledger.data?.items ?? [])], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `billing-page-${page}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <FileText />
            Billing
          </PageHeaderTitle>
          <PageHeaderDescription>
            Credits, settled usage charges, and released reservations for your account.
          </PageHeaderDescription>
        </PageHeader>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={ledger.isFetching || !ledger.data?.items.length} onClick={exportPage}>
            <Download /> Export current page
          </Button>
          <Button variant="outline" disabled={ledger.isFetching} onClick={() => void ledger.refetch()}>
            <RefreshCw className={ledger.isFetching ? "animate-spin" : ""} />
            Refresh
          </Button>
        </div>
      </div>
      <div className="overflow-hidden rounded-xl border">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-4">
          <Select
            items={transactionTypes.map((value) => ({ value, label: typeLabels[value] }))}
            value={filters.type}
            onValueChange={(value) => {
              if (value && transactionTypes.includes(value)) void setFilters({ type: value, page: 1 });
            }}
          >
            <SelectTrigger aria-label="Transaction type" className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {transactionTypes.map((type) => (
                <SelectItem key={type} value={type}>
                  {typeLabels[type]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">All amounts in USD · Dates in your local timezone</p>
        </div>
        <LedgerTable entries={ledger.data?.items ?? []} loading={ledger.isPending} error={ledger.error} />
        <div className="flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-xs text-muted-foreground">
          <span>
            {total === 0
              ? "No results"
              : `${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, total)} of ${total} transactions`}
          </span>
          <div className="flex items-center gap-3">
            <span>
              Page {page} of {pageCount}
            </span>
            <Button
              variant="outline"
              size="icon-xs"
              aria-label="Previous page"
              disabled={page <= 1 || ledger.isFetching}
              onClick={() => void setFilters({ page: page - 1 })}
            >
              <ChevronLeft />
            </Button>
            <Button
              variant="outline"
              size="icon-xs"
              aria-label="Next page"
              disabled={page >= pageCount || ledger.isFetching}
              onClick={() => void setFilters({ page: page + 1 })}
            >
              <ChevronRight />
            </Button>
          </div>
        </div>
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">
        Billing lists settled customer charges after your model and resource pool discounts.
      </p>
    </div>
  );
}
