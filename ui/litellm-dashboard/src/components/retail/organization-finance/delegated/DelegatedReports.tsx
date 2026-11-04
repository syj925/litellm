"use client";

import { useMutation, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { useQueryState } from "nuqs";
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { RetailRequestError, formatMoney, retailRequest, type PageResult } from "../../api";
import {
  ManagementPagination,
  managementAttempt,
  managementPageParser,
  managementQuery,
  managementTimestamp,
  type IdempotencyAttempt,
} from "../../management/ManagementShared";
import { organizationReportLabels, organizationReportPending } from "../organizationReportInput";
import DelegatedReportDetails from "./DelegatedReportDetails";
import { DelegatedError } from "./DelegatedShared";
import { delegatedAccessLost } from "./input";
import { downloadDelegatedReport } from "./download";
import type { DelegatedConsumption, DelegatedReport, DelegatedReportCreate, FinanceAccess } from "./types";

function availableReports(data: PageResult<DelegatedReport> | undefined, error: Error | null, lost: boolean) {
  if (error || lost) return undefined;
  return data;
}

export default function DelegatedReports({
  access,
  preview,
  accessToken,
  actorId,
  instance,
  onAccessRefresh,
}: {
  access: FinanceAccess;
  preview: DelegatedConsumption | undefined;
  accessToken: string;
  actorId: string | null;
  instance: string;
  onAccessRefresh: () => void;
}) {
  const client = useQueryClient();
  const [page, setPage] = useQueryState("export_page", managementPageParser);
  const [lost, setLost] = useState(false);
  const [created, setCreated] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const prefix = [
    "retail",
    "delegated-finance",
    actorId,
    access.organization_id,
    access.access_revision,
    instance,
    "reports",
  ];
  const path = `/organizations/${encodeURIComponent(access.organization_id)}/finance/reports`;
  const options = {
    queryKey: [...prefix, "list", page],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const result = await retailRequest<PageResult<DelegatedReport>>(
        managementQuery(path, { limit: 25, offset: (page - 1) * 25 }),
        accessToken,
        { signal },
      );
      const valid = result.items.every(
        (job) =>
          job.scope === "delegated_organization" &&
          job.organization_id === access.organization_id &&
          job.access_revision === access.access_revision,
      );
      if (!valid) throw new RetailRequestError("Authorization changed", 403);
      return result;
    },
    enabled: access.can_export && access.can_view && !lost,
    retry: false,
    refetchInterval: (query: Query<PageResult<DelegatedReport>>): number | false => {
      if (query.state.error) return false;
      return query.state.data?.items.some(organizationReportPending) ? 2000 : false;
    },
  };
  const jobs = useQuery(options);
  useEffect(() => {
    if (delegatedAccessLost(jobs.error)) onAccessRefresh();
  }, [jobs.error, onAccessRefresh]);
  const refetchJobs = jobs.refetch;
  const refresh = useCallback(() => {
    void refetchJobs();
  }, [refetchJobs]);
  const unavailable = useCallback(() => {
    setLost(true);
    setSelected(null);
    onAccessRefresh();
  }, [onAccessRefresh]);
  const createOptions = {
    mutationFn: async (payload: DelegatedReportCreate) => {
      const result = await retailRequest<DelegatedReport>(path, accessToken, {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
      });
      const valid =
        result.scope === "delegated_organization" &&
        result.organization_id === access.organization_id &&
        result.access_revision === access.access_revision;
      if (!valid) throw new RetailRequestError("Authorization changed", 403);
      return result;
    },
    onSuccess: (job: DelegatedReport) => {
      attempt.current = null;
      setCreated(true);
      setSelected(job.id);
      void setPage(1);
      refresh();
    },
    onError: (error: Error) => {
      if (delegatedAccessLost(error)) unavailable();
    },
  };
  const create = useMutation(createOptions);
  const downloadOptions = {
    mutationFn: (id: string) => downloadDelegatedReport(access.organization_id, id, accessToken),
    onError: (error: Error) => {
      if (error instanceof RetailRequestError && [401, 403].includes(error.status)) unavailable();
      else {
        refresh();
        onAccessRefresh();
      }
    },
  };
  const download = useMutation(downloadOptions);
  const lostAccess = lost || delegatedAccessLost(jobs.error);
  const visible = availableReports(jobs.data, jobs.error, lostAccess);
  const ready = Boolean(visible) && Boolean(preview);
  const canCreate = ready && !create.isPending && !created;
  const createSnapshot = () => {
    if (!canCreate || !preview || preview.access_revision !== access.access_revision) return;
    const payload: DelegatedReportCreate = {
      start_at: preview.start,
      end_at: preview.end,
      currency: "USD",
      timezone: "Asia/Shanghai",
    };
    create.mutate(payload);
  };
  const creationLabel = created ? "Snapshot task created" : "Create authorized CSV";
  const refreshList = () => void client.invalidateQueries({ queryKey: [...prefix, "list"] });
  return (
    <section className="space-y-4 border-t pt-5" aria-label="Authorized finance exports">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h3 className="font-semibold">Fixed finance snapshots</h3>
          <p className="max-w-prose text-sm text-muted-foreground">
            Exports freeze the applied window and outcomes. Earlier authorization cannot be used after access is revoked
            and granted again.
          </p>
        </div>
        <Button variant="outline" onClick={refresh} disabled={jobs.isFetching}>
          Reload snapshots
        </Button>
      </div>
      {lostAccess ? (
        <p role="alert" className="text-sm text-destructive">
          Export access changed. Reload access to verify your current permission.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap gap-3">
            <Button disabled={!canCreate} onClick={createSnapshot}>
              {create.isPending ? "Creating snapshot…" : creationLabel}
            </Button>
            {created && (
              <Button
                variant="ghost"
                onClick={() => {
                  setCreated(false);
                  create.reset();
                }}
              >
                Start a new snapshot
              </Button>
            )}
          </div>
          {create.error && <DelegatedError error={create.error} />}
          {download.error && <DelegatedError error={download.error} />}
          {jobs.error && <DelegatedError error={jobs.error} />}
          {jobs.isPending && <p role="status">Loading authorized snapshots…</p>}
          {visible && (
            <div className="overflow-hidden rounded-xl border">
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Accepted window</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Expires (Beijing)</TableHead>
                      <TableHead className="text-right">Final charges (USD)</TableHead>
                      <TableHead>
                        <span className="sr-only">Actions</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {visible.items.map((job) => (
                      <TableRow key={job.id}>
                        <TableCell className="text-xs">
                          {managementTimestamp(job.start_at)}
                          <br />
                          before {managementTimestamp(job.end_at)}
                        </TableCell>
                        <TableCell>
                          <Badge variant={job.state === "ready" ? "secondary" : "outline"}>
                            {organizationReportLabels[job.state]}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs">{managementTimestamp(job.expires_at)}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {job.state === "ready" ? formatMoney(job.summary.settled_usd) : "Not finalized"}
                        </TableCell>
                        <TableCell>
                          <div className="flex justify-end gap-2">
                            <Button size="sm" variant="outline" onClick={() => setSelected(job.id)}>
                              Details
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              aria-label={`Download authorized snapshot ${job.id}`}
                              disabled={job.state !== "ready" || download.isPending}
                              onClick={() => download.mutate(job.id)}
                            >
                              CSV
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              {visible.total === 0 && (
                <p role="status" className="p-4 text-sm text-muted-foreground">
                  No snapshots in your current authorization.
                </p>
              )}
              <ManagementPagination
                page={page}
                total={visible.total}
                loading={jobs.isFetching}
                onChange={(page) => void setPage(page)}
              />
            </div>
          )}
          {visible && selected && (
            <DelegatedReportDetails
              id={selected}
              access={access}
              accessToken={accessToken}
              queryPrefix={prefix}
              onClose={() => {
                setSelected(null);
                refreshList();
              }}
              onUnavailable={unavailable}
            />
          )}
        </>
      )}
    </section>
  );
}
