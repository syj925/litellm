"use client";

import { useMutation, useQuery, type Query } from "@tanstack/react-query";
import { Download, FileText, RefreshCw } from "lucide-react";
import { useQueryState } from "nuqs";
import { useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { isProxyAdminRole } from "@/utils/roles";
import { formatMoney, retailRequest, type PageResult } from "../api";
import {
  ManagementError,
  ManagementFeatureGate,
  ManagementPagination,
  managementPageParser,
  managementPageSize,
  managementQuery,
  managementTimestamp,
} from "../management/ManagementShared";
import { downloadRetailReport } from "./download";
import RetailReportForm from "./RetailReportForm";
import type { RetailReport, RetailReportState } from "./types";

const reportStateLabels: Record<RetailReportState, string> = {
  queued: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
  revoked: "Access revoked",
  expired: "Expired",
};
function pending(report: RetailReport): boolean {
  return report.state === "queued" || report.state === "processing";
}
function unresolved(report: RetailReport): number {
  return report.unresolved.pending + report.unresolved.unknown + report.unresolved.conflict;
}

function ReportDetails({
  id,
  actorId,
  accessToken,
  onClose,
}: {
  id: string;
  actorId: string | null;
  accessToken: string;
  onClose: () => void;
}) {
  const reportOptions = {
    queryKey: ["retail", "reports", actorId, "detail", id],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<RetailReport>(`/reports/${encodeURIComponent(id)}`, accessToken, { signal }),
    refetchInterval: (query: Query<RetailReport>): number | false => {
      if (query.state.error) return false;
      return query.state.data && pending(query.state.data) ? 2000 : false;
    },
    retry: false,
  };
  const report = useQuery(reportOptions);
  const data = report.error ? undefined : report.data;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Wallet report details</DialogTitle>
          <DialogDescription>Fixed account scope and captured ledger snapshot.</DialogDescription>
        </DialogHeader>
        {report.isPending && <LoadingState description="Loading authorized gateway data." title="Loading report" />}
        {report.error && <ManagementError title="Unable to load report" error={report.error} />}
        {data && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={data.state === "ready" ? "secondary" : "outline"}>{reportStateLabels[data.state]}</Badge>
              {data.partial && <Badge variant="outline">Partial snapshot</Badge>}
              <span className="text-xs text-muted-foreground">{data.total_rows.toLocaleString()} rows</span>
            </div>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-5 gap-y-3 text-sm">
              <dt className="text-muted-foreground">Report ID</dt>
              <dd className="break-all font-mono text-xs">{data.id}</dd>
              <dt className="text-muted-foreground">Account ID</dt>
              <dd className="break-all font-mono text-xs">{data.account_id}</dd>
              <dt className="text-muted-foreground">From (Beijing)</dt>
              <dd>{managementTimestamp(data.start_at)}</dd>
              <dt className="text-muted-foreground">Until (exclusive)</dt>
              <dd>{managementTimestamp(data.end_at)}</dd>
              <dt className="text-muted-foreground">Snapshot (Beijing)</dt>
              <dd>{managementTimestamp(data.snapshot_at)}</dd>
              <dt className="text-muted-foreground">Expires (Beijing)</dt>
              <dd>{managementTimestamp(data.expires_at)}</dd>
              <dt className="text-muted-foreground">Pending requests</dt>
              <dd>{data.unresolved.pending}</dd>
              <dt className="text-muted-foreground">Unknown usage</dt>
              <dd>{data.unresolved.unknown}</dd>
              <dt className="text-muted-foreground">Conflicting facts</dt>
              <dd>{data.unresolved.conflict}</dd>
            </dl>
            {data.state === "ready" && (
              <div className="grid gap-3 rounded-lg border bg-muted/20 p-4 sm:grid-cols-3">
                <div>
                  <p className="text-xs text-muted-foreground">Credits (USD)</p>
                  <p className="mt-1 font-semibold tabular-nums">{formatMoney(data.totals.credit)}</p>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Usage charges (USD)</p>
                  <p className="mt-1 font-semibold tabular-nums">{formatMoney(data.totals.charge)}</p>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Releases (USD)</p>
                  <p className="mt-1 font-semibold tabular-nums">{formatMoney(data.totals.release)}</p>
                </div>
              </div>
            )}
            {data.partial && (
              <Alert variant="warning">
                <AlertDescription>
                    This window includes the current Beijing day or a future interval. Later transactions are not
                    included in this snapshot.
                </AlertDescription>
              </Alert>
            )}
            {unresolved(data) > 0 && (
              <Alert variant="warning">
                <AlertDescription>
                  Unresolved requests are listed separately and are excluded from the settled totals. Review request
                  status before treating this period as reconciled.
                </AlertDescription>
              </Alert>
            )}
            {data.state === "failed" && (
              <Alert variant="error">
                <AlertTitle>Report generation failed</AlertTitle>
                <AlertDescription>
                  {data.error_code ?? "Refresh the list or create a new report with a smaller window."}
                </AlertDescription>
              </Alert>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function ReportsWorkspace() {
  const { accessToken, userId, userRole, isViewOnly } = useAuthorized();
  const readOnly = isViewOnly || userRole.toLowerCase().replaceAll(" ", "_").endsWith("viewer");
  const admin = isProxyAdminRole(userRole) && !readOnly;
  const [page, setPage] = useQueryState("page", managementPageParser);
  const [selected, setSelected] = useState<string | null>(null);
  const reportListOptions = {
    queryKey: ["retail", "reports", userId, "list", page],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<RetailReport>>(
        managementQuery("/reports", { limit: managementPageSize, offset: (page - 1) * managementPageSize }),
        accessToken,
        { signal },
      ),
    enabled: Boolean(accessToken),
    refetchInterval: (query: Query<PageResult<RetailReport>>): number | false => {
      if (query.state.error) return false;
      return query.state.data?.items.some(pending) ? 2000 : false;
    },
    retry: false,
  };
  const reports = useQuery(reportListOptions);
  const download = useMutation({
    mutationFn: (report: RetailReport) => downloadRetailReport(report.id, accessToken ?? ""),
  });
  const showDetails = !reports.error ? selected : null;
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <FileText />
            Reports
          </PageHeaderTitle>
          <PageHeaderDescription>
            Create account-scoped CSV exports of retail wallet activity and track their readiness.
          </PageHeaderDescription>
        </PageHeader>
        <Button variant="outline" disabled={reports.isFetching} onClick={() => void reports.refetch()}>
          <RefreshCw />
          Refresh reports
        </Button>
      </div>
      <Alert variant="info">
        <AlertDescription>
          These reports contain wallet ledger credits, usage charges, and released reservations. They do not represent
          provider cost or profit. Each report uses a fixed captured snapshot.
        </AlertDescription>
      </Alert>
      {readOnly && (
        <Alert variant="info">
          <AlertDescription>
            Your account has read-only access. You can view existing reports but cannot create new exports.
          </AlertDescription>
        </Alert>
      )}
      {accessToken && !readOnly && (
        <RetailReportForm
          accessToken={accessToken}
          admin={admin}
          onCreated={(report) => {
            setSelected(report.id);
            void setPage(1);
          }}
        />
      )}
      {download.error && <ManagementError title="Unable to download report" error={download.error} />}
      <section className="overflow-hidden rounded-xl border" aria-label="Your reports">
        <div className="border-b px-5 py-4">
          <h2 className="font-medium">Your report tasks</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Only report tasks created by your account are listed here.
          </p>
        </div>
        {reports.isPending && (
          <LoadingState description="Loading authorized gateway data." title="Loading report tasks" />
        )}
        {reports.error && <ManagementError title="Unable to load report tasks" error={reports.error} />}
        {reports.data && !reports.error && (
          <>
            {reports.data.items.length === 0 ? (
              <StateMessage
                role="status"
                icon={<FileText className="size-5" />}
                title="No reports yet"
                description="Choose a time window above to create your first wallet report."
              />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-5">Created (Beijing)</TableHead>
                    <TableHead>Account</TableHead>
                    <TableHead>Time window (Beijing)</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Rows</TableHead>
                    <TableHead className="text-right">Charges (USD)</TableHead>
                    <TableHead className="pr-5">
                      <span className="sr-only">Actions</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {reports.data.items.map((report) => (
                    <TableRow key={report.id}>
                      <TableCell className="pl-5 text-xs text-muted-foreground">
                        {managementTimestamp(report.created_at)}
                      </TableCell>
                      <TableCell className="max-w-36 truncate font-mono text-xs">{report.account_id}</TableCell>
                      <TableCell className="text-xs">
                        <div>{managementTimestamp(report.start_at)}</div>
                        <div className="mt-1 text-muted-foreground">until {managementTimestamp(report.end_at)}</div>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          <Badge variant={report.state === "ready" ? "secondary" : "outline"}>
                            {reportStateLabels[report.state]}
                          </Badge>
                          {report.partial && <Badge variant="outline">Partial</Badge>}
                          {unresolved(report) > 0 && <Badge variant="outline">Unresolved requests</Badge>}
                        </div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {report.state === "ready" ? report.total_rows.toLocaleString() : "��"}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {report.state === "ready" ? formatMoney(report.totals.charge) : "Not finalized"}
                      </TableCell>
                      <TableCell className="pr-5">
                        <div className="flex justify-end gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            aria-label={`View report ${report.id}`}
                            onClick={() => setSelected(report.id)}
                          >
                            Details
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            aria-label={`Download report ${report.id}`}
                            disabled={report.state !== "ready" || download.isPending}
                            onClick={() => download.mutate(report)}
                          >
                            <Download />
                            CSV
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            <ManagementPagination
              page={page}
              total={reports.data.total}
              loading={reports.isFetching}
              onChange={(next) => void setPage(next)}
            />
          </>
        )}
      </section>
      {showDetails && accessToken && (
        <ReportDetails id={showDetails} actorId={userId} accessToken={accessToken} onClose={() => setSelected(null)} />
      )}
    </>
  );
}

export default function RetailReportsPanel() {
  return (
    <Page>
      <ManagementFeatureGate feature="report_export_enabled" title="Report export">
        <ReportsWorkspace />
      </ManagementFeatureGate>
    </Page>
  );
}
