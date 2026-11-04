"use client";

import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { managementTimestamp } from "../management/ManagementShared";
import { canDiscardDeliveries, canEnableDelivery, deliveryStateLabels, outstandingDeliveries } from "./deliveryState";
import { deliveryStates, type AuditDeliveryStatus, type DeliveryCommand } from "./types";

function ConfigurationNotice({ status }: { status: AuditDeliveryStatus }) {
  if (!status.destination_configured || status.configuration_error !== null) {
    return (
      <Alert variant="info">
        <AlertTitle>Operator configuration required</AlertTitle>
        <AlertDescription>
          Configure a valid receiving endpoint and signing secret on the server before enabling delivery. This page does
          not accept or display addresses and secrets. Pausing or discarding existing work remains available.
        </AlertDescription>
      </Alert>
    );
  }
  if (!status.configuration_matches && outstandingDeliveries(status) > 0) {
    return (
      <Alert variant="warning">
        <AlertTitle>Receiver configuration changed</AlertTitle>
        <AlertDescription>
          Outstanding deliveries belong to the previous configuration. Restore it to finish delivery, or pause and
          explicitly discard the outstanding work before enabling the new configuration.
        </AlertDescription>
      </Alert>
    );
  }
  return null;
}

export default function DeliveryStatus({
  status,
  busy,
  onCommand,
}: {
  status: AuditDeliveryStatus;
  busy: boolean;
  onCommand: (command: DeliveryCommand) => void;
}) {
  const [discarding, setDiscarding] = useState(false);
  return (
    <section className="space-y-5" aria-label="Audit delivery configuration">
      <ConfigurationNotice status={status} />
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold">{status.enabled ? "Delivery enabled" : "Delivery paused"}</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            New external work follows this setting. Pausing cannot recall a request already sent to the receiver.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant={status.enabled ? "outline" : "default"}
            disabled={busy || (!status.enabled && !canEnableDelivery(status))}
            onClick={() =>
              onCommand({ kind: "configure", enabled: !status.enabled, expected_version: status.config_version })
            }
          >
            {status.enabled ? "Pause delivery" : "Enable delivery"}
          </Button>
          <Button
            variant="outline"
            disabled={busy || !canDiscardDeliveries(status)}
            onClick={() => setDiscarding(true)}
          >
            Discard outstanding
          </Button>
        </div>
      </div>
      <dl className="grid gap-x-8 gap-y-4 border-y py-5 sm:grid-cols-3 xl:grid-cols-5">
        {deliveryStates.map((state) => (
          <div key={state} className="flex items-baseline justify-between gap-3 text-sm">
            <dt className="text-muted-foreground">{deliveryStateLabels[state]}</dt>
            <dd className="font-medium tabular-nums">{status.counts[state].toLocaleString()}</dd>
          </div>
        ))}
      </dl>
      {status.overflow_count > 0 && (
        <Alert variant="warning">
          <AlertTitle>External delivery gaps recorded</AlertTitle>
          <AlertDescription>
            {status.overflow_count.toLocaleString()} events did not enter the full external queue. Their local audit
            records remain saved. Retrying the rows below does not recover these gaps.
            {status.last_overflow_at && (
              <span className="block mt-1">Latest gap: {managementTimestamp(status.last_overflow_at)} (Beijing)</span>
            )}
          </AlertDescription>
        </Alert>
      )}
      <details className="text-sm">
        <summary className="w-fit cursor-pointer font-medium underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-4">
          Delivery scope and limits
        </summary>
        <div className="mt-3 max-w-prose space-y-2 leading-relaxed text-muted-foreground">
          <p>
            This receiver gets supported management-event metadata only: stable event and delivery identifiers, action,
            object type, outcome, versions and time. It receives no actor or object IDs, changed values, request
            content, credentials or internal diagnostics. Request logs, wallet charges and other native operations are
            not mirrored.
          </p>
          <p>
            Accepted work may be delivered more than once. The receiver must deduplicate by delivery ID. Up to{" "}
            {status.global_concurrency} deliveries run at once, with {status.max_attempts} automatic attempts. A manual
            retry within the delivery lifetime keeps the same ID and restarts the attempt count. Expired work cannot be
            retried; its local audit event remains saved.
          </p>
          <p>
            The queue and receipts hold at most {status.queue_capacity.toLocaleString()} records. Completed history is
            eligible for cleanup after {status.retention_days} days while a configured worker is running, and may be
            removed sooner under pressure. Missing configuration or an outage can defer cleanup. Overflow skips
            external delivery and records a gap without removing the local audit event.
          </p>
        </div>
      </details>
      {discarding && (
        <Dialog open onOpenChange={(open) => !busy && setDiscarding(open)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Discard outstanding deliveries?</DialogTitle>
              <DialogDescription>
                This marks {outstandingDeliveries(status).toLocaleString()} queued, sending or failed records as
                discarded. Their local audit events remain saved. Requests already sent to the receiver cannot be
                recalled.
              </DialogDescription>
            </DialogHeader>
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="outline" disabled={busy} onClick={() => setDiscarding(false)}>
                Keep deliveries
              </Button>
              <Button
                variant="destructive"
                disabled={busy || !canDiscardDeliveries(status)}
                onClick={() => {
                  onCommand({
                    kind: "discard",
                    expected_version: status.config_version,
                    confirmation: "discard_pending",
                  });
                  setDiscarding(false);
                }}
              >
                Discard outstanding deliveries
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </section>
  );
}
