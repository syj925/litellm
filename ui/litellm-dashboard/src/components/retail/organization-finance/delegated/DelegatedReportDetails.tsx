"use client";

import { useQuery, type Query } from "@tanstack/react-query";
import { useEffect } from "react";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { RetailRequestError, formatMoney, retailRequest } from "../../api";
import { managementTimestamp } from "../../management/ManagementShared";
import { organizationReportLabels, organizationReportPending } from "../organizationReportInput";
import { DelegatedError } from "./DelegatedShared";
import { delegatedAccessLost } from "./input";
import type { DelegatedReport, FinanceAccess } from "./types";

export default function DelegatedReportDetails({
  id,
  access,
  accessToken,
  queryPrefix,
  onClose,
  onUnavailable,
}: {
  id: string;
  access: FinanceAccess;
  accessToken: string;
  queryPrefix: readonly unknown[];
  onClose: () => void;
  onUnavailable: () => void;
}) {
  const options = {
    queryKey: [...queryPrefix, "detail", id],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const result = await retailRequest<DelegatedReport>(
        `/organizations/${encodeURIComponent(access.organization_id)}/finance/reports/${encodeURIComponent(id)}`,
        accessToken,
        { signal },
      );
      if (
        result.organization_id !== access.organization_id ||
        result.access_revision !== access.access_revision ||
        result.scope !== "delegated_organization"
      )
        throw new RetailRequestError("Authorization changed", 403);
      return result;
    },
    retry: false,
    refetchInterval: (query: Query<DelegatedReport>): number | false => {
      if (query.state.error) return false;
      return query.state.data && organizationReportPending(query.state.data) ? 2000 : false;
    },
  };
  const job = useQuery(options);
  const visible = !job.error ? job.data : undefined;
  useEffect(() => {
    if (delegatedAccessLost(job.error)) onUnavailable();
  }, [job.error, onUnavailable]);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Authorized finance snapshot</DialogTitle>
          <DialogDescription>
            Outcomes frozen when this export was created under your current authorization.
          </DialogDescription>
        </DialogHeader>
        {job.isPending && <p role="status">Loading snapshot…</p>}
        {job.error && <DelegatedError error={job.error} />}
        {visible && (
          <>
            <Badge variant={visible.state === "ready" ? "secondary" : "outline"}>
              {organizationReportLabels[visible.state]}
            </Badge>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-5 gap-y-3 text-sm">
              <dt>Organization</dt>
              <dd>{visible.organization_name}</dd>
              <dt>Accepted from</dt>
              <dd>{managementTimestamp(visible.start_at)} (Beijing)</dd>
              <dt>Accepted before</dt>
              <dd>{managementTimestamp(visible.end_at)} (Beijing)</dd>
              <dt>Snapshot captured</dt>
              <dd>{managementTimestamp(visible.snapshot_at)} (Beijing)</dd>
              <dt>Download expires</dt>
              <dd>{managementTimestamp(visible.expires_at)} (Beijing)</dd>
              {visible.state === "ready" && (
                <>
                  <dt>Finalized retail charges</dt>
                  <dd className="tabular-nums">{formatMoney(visible.summary.settled_usd)}</dd>
                  <dt>Pending / unknown / conflict</dt>
                  <dd className="tabular-nums">
                    {visible.summary.pending_requests} / {visible.summary.unknown_requests} /{" "}
                    {visible.summary.conflict_requests}
                  </dd>
                </>
              )}
            </dl>
            <p className="text-sm text-muted-foreground">
              Later settlements do not change this snapshot. Revoked authorization cannot be used to reopen earlier
              exports.
            </p>
            {visible.state === "failed" && (
              <p role="alert" className="text-sm text-destructive">
                Snapshot generation failed. Create another task with a smaller window.
              </p>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
