"use client";

import { useQuery, type Query } from "@tanstack/react-query";
import { useEffect } from "react";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatMoney, retailRequest } from "../api";
import { managementTimestamp } from "../management/ManagementShared";
import { financeAccessLost } from "./financeInput";
import {
  organizationReportError,
  organizationReportLabels,
  organizationReportPending,
  organizationReportRefreshNeeded,
} from "./organizationReportInput";
import type { OrganizationReportJob } from "./types";

export default function OrganizationReportDetails({
  organizationId,
  id,
  actorId,
  accessToken,
  onClose,
  onUnavailable,
  onRefreshList,
}: {
  organizationId: string;
  id: string;
  actorId: string | null;
  accessToken: string;
  onClose: () => void;
  onUnavailable: () => void;
  onRefreshList: () => void;
}) {
  const options = {
    queryKey: ["retail", "organization-report-jobs", actorId, organizationId, "detail", id],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<OrganizationReportJob>(
        `/admin/organizations/${encodeURIComponent(organizationId)}/reports/${encodeURIComponent(id)}`,
        accessToken,
        { signal },
      ),
    retry: false,
    refetchInterval: (query: Query<OrganizationReportJob>): number | false => {
      if (query.state.error) return false;
      return query.state.data && organizationReportPending(query.state.data) ? 2000 : false;
    },
  };
  const job = useQuery(options);
  const visible = job.error ? undefined : job.data;
  useEffect(() => {
    if (financeAccessLost(job.error)) onUnavailable();
    else if (job.error && organizationReportRefreshNeeded(job.error)) onRefreshList();
  }, [job.error, onUnavailable, onRefreshList]);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Organization CSV snapshot</DialogTitle>
          <DialogDescription>
            Fixed request admission window and outcomes captured when this task was created.
          </DialogDescription>
        </DialogHeader>
        {job.isPending && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading snapshot task…
          </p>
        )}
        {job.error && (
          <p role="alert" className="text-sm text-destructive">
            {organizationReportError(job.error)}
          </p>
        )}
        {visible && (
          <>
            <div className="flex flex-wrap gap-2">
              <Badge variant={visible.state === "ready" ? "secondary" : "outline"}>
                {organizationReportLabels[visible.state]}
              </Badge>
              {visible.partial && <Badge variant="outline">Includes an open day</Badge>}
            </div>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-5 gap-y-3 text-sm">
              <dt className="text-muted-foreground">Organization</dt>
              <dd className="break-words">{visible.organization_name}</dd>
              <dt className="text-muted-foreground">Task ID</dt>
              <dd className="break-all font-mono text-xs">{visible.id}</dd>
              <dt className="text-muted-foreground">Accepted from</dt>
              <dd>{managementTimestamp(visible.start_at)} (Beijing)</dd>
              <dt className="text-muted-foreground">Accepted before</dt>
              <dd>{managementTimestamp(visible.end_at)} (Beijing)</dd>
              <dt className="text-muted-foreground">Snapshot captured</dt>
              <dd>{managementTimestamp(visible.snapshot_at)} (Beijing)</dd>
              <dt className="text-muted-foreground">Download expires</dt>
              <dd>{managementTimestamp(visible.expires_at)} (Beijing)</dd>
            </dl>
            {visible.state === "ready" && (
              <section className="space-y-3 border-t pt-4" aria-label="Frozen request outcomes">
                <h3 className="text-sm font-medium">Frozen request outcomes</h3>
                <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
                  <div>
                    <dt className="text-muted-foreground">Accepted</dt>
                    <dd className="tabular-nums">{visible.summary.accepted_requests.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Settled</dt>
                    <dd className="tabular-nums">{visible.summary.settled_requests.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Released</dt>
                    <dd className="tabular-nums">{visible.summary.released_requests.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Pending</dt>
                    <dd className="tabular-nums">{visible.summary.pending_requests.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Unknown</dt>
                    <dd className="tabular-nums">{visible.summary.unknown_requests.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Conflict</dt>
                    <dd className="tabular-nums">{visible.summary.conflict_requests.toLocaleString()}</dd>
                  </div>
                </dl>
                <p className="text-sm">
                  Finalized retail charges (USD):{" "}
                  <span className="font-semibold tabular-nums">{formatMoney(visible.summary.settled_usd)}</span>
                </p>
              </section>
            )}
            {visible.state === "failed" && (
              <p role="alert" className="text-sm text-destructive">
                Snapshot generation failed. Create a new task or use a smaller window.
              </p>
            )}
            <p className="max-w-prose text-sm text-muted-foreground">
              Later settlements do not update this snapshot. Pending, unknown, and conflicting outcomes stay separate
              from finalized charges. Create a new task to capture newer outcomes.
            </p>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
