"use client";

import { Download } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatMoney, type PageResult } from "../api";
import { ManagementPagination, managementTimestamp } from "../management/ManagementShared";
import { organizationReportError, organizationReportLabels } from "./organizationReportInput";
import type { OrganizationReportJob } from "./types";

export function OrganizationReportList({
  data,
  loading,
  fetching,
  error,
  page,
  downloading,
  onPage,
  onDetails,
  onDownload,
}: {
  data: PageResult<OrganizationReportJob> | undefined;
  loading: boolean;
  fetching: boolean;
  error: Error | null;
  page: number;
  downloading: boolean;
  onPage: (page: number) => void;
  onDetails: (id: string) => void;
  onDownload: (job: OrganizationReportJob) => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border">
      <div className="space-y-1 border-b px-4 py-3">
        <h3 className="text-sm font-medium">Your tasks for this organization</h3>
        <p className="text-xs text-muted-foreground">
          Only tasks created by your account appear here. Each download checks your current access.
        </p>
      </div>
      {loading && (
        <p role="status" className="p-4 text-sm text-muted-foreground">
          Loading export tasks…
        </p>
      )}
      {error && (
        <p role="alert" className="p-4 text-sm text-destructive">
          {organizationReportError(error)}
        </p>
      )}
      {data && (
        <>
          {data.items.length ? (
            <OrganizationReportsTable
              jobs={data.items}
              downloading={downloading}
              onDetails={onDetails}
              onDownload={onDownload}
            />
          ) : (
            <p role="status" className="p-4 text-sm text-muted-foreground">
              No fixed export tasks for this organization yet.
            </p>
          )}
          <ManagementPagination page={page} total={data.total} loading={fetching} onChange={onPage} />
        </>
      )}
    </div>
  );
}

export default function OrganizationReportsTable({
  jobs,
  downloading,
  onDetails,
  onDownload,
}: {
  jobs: OrganizationReportJob[];
  downloading: boolean;
  onDetails: (id: string) => void;
  onDownload: (job: OrganizationReportJob) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Created (Beijing)</TableHead>
            <TableHead>Accepted window (Beijing)</TableHead>
            <TableHead>State</TableHead>
            <TableHead className="text-right">Rows</TableHead>
            <TableHead className="text-right">Final charges (USD)</TableHead>
            <TableHead>Download expires</TableHead>
            <TableHead>
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {jobs.map((job) => (
            <TableRow key={job.id}>
              <TableCell className="whitespace-nowrap text-xs">{managementTimestamp(job.created_at)}</TableCell>
              <TableCell className="whitespace-nowrap text-xs">
                <p>{managementTimestamp(job.start_at)}</p>
                <p className="mt-1 text-muted-foreground">before {managementTimestamp(job.end_at)}</p>
              </TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-1">
                  <Badge variant={job.state === "ready" ? "secondary" : "outline"}>
                    {organizationReportLabels[job.state]}
                  </Badge>
                  {job.partial && <Badge variant="outline">Open day</Badge>}
                </div>
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {job.state === "ready" ? job.total_rows.toLocaleString() : "Not finalized"}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {job.state === "ready" ? formatMoney(job.summary.settled_usd) : "Not finalized"}
              </TableCell>
              <TableCell className="whitespace-nowrap text-xs">{managementTimestamp(job.expires_at)}</TableCell>
              <TableCell>
                <div className="flex justify-end gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    aria-label={`View organization export ${job.id}`}
                    onClick={() => onDetails(job.id)}
                  >
                    Details
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    aria-label={`Download organization export ${job.id}`}
                    disabled={job.state !== "ready" || downloading}
                    onClick={() => onDownload(job)}
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
    </div>
  );
}
