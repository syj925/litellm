"use client";

import CopyButton from "@/components/shared/CopyButton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { eventLabels } from "../audit/eventLabels";
import { managementTimestamp } from "../management/ManagementShared";
import { canRetryDelivery, deliveryErrorText, deliveryStateLabels } from "./deliveryState";
import type { AuditDeliveryJob, AuditDeliveryStatus } from "./types";

export default function DeliveryTable({
  rows,
  status,
  busy,
  onRetry,
}: {
  rows: AuditDeliveryJob[];
  status: AuditDeliveryStatus;
  busy: boolean;
  onRetry: (job: AuditDeliveryJob) => void;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-5">Event (Beijing)</TableHead>
          <TableHead>Action</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Attempts</TableHead>
          <TableHead>Latest result</TableHead>
          <TableHead className="pr-5">
            <span className="sr-only">Actions</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((job) => (
          <TableRow key={job.id}>
            <TableCell className="min-w-44 pl-5">
              <p className="text-xs">{managementTimestamp(job.occurred_at)}</p>
              <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
                <span className="font-mono">{job.event_id.slice(0, 8)}</span>
                <CopyButton value={job.event_id} label={`Copy event ID ${job.event_id}`} />
              </div>
            </TableCell>
            <TableCell className="max-w-64">{eventLabels[job.action] ?? "Recorded management event"}</TableCell>
            <TableCell>
              <Badge variant="outline">{deliveryStateLabels[job.state]}</Badge>
              {!job.configuration_matches && (
                <p className="mt-1 text-xs text-muted-foreground">Previous target configuration</p>
              )}
            </TableCell>
            <TableCell className="tabular-nums">
              {job.attempts} / {status.max_attempts}
            </TableCell>
            <TableCell className="min-w-52 max-w-72 text-xs text-muted-foreground">
              <p>{job.state === "delivered" ? "Accepted by the receiver" : deliveryErrorText(job.error_code)}</p>
              {job.state === "pending" && job.next_attempt_at && (
                <p className="mt-1">Next attempt: {managementTimestamp(job.next_attempt_at)}</p>
              )}
            </TableCell>
            <TableCell className="pr-5 text-right">
              <Button
                variant="outline"
                size="sm"
                disabled={busy || !canRetryDelivery(job, status)}
                onClick={() => onRetry(job)}
                aria-label={`Retry delivery ${job.id}`}
              >
                Retry
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
