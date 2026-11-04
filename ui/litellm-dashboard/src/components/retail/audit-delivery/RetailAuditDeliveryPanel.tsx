"use client";

import { useMutation, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw, Send } from "lucide-react";
import Link from "next/link";
import { useQueryStates } from "nuqs";
import { useId, useRef, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button, buttonVariants } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { uiHref } from "@/utils/uiHref";
import { retailRequest, type PageResult } from "../api";
import {
  ManagementFeatureGate,
  ManagementPagination,
  managementAttempt,
  managementConflict,
  managementQuery,
  type IdempotencyAttempt,
} from "../management/ManagementShared";
import DeliveryStatus from "./DeliveryStatus";
import DeliveryTable from "./DeliveryTable";
import {
  deliveryAccessLost,
  deliveryParsers,
  deliveryRequest,
  deliveryRequestError,
  deliveryStateLabels,
} from "./deliveryState";
import { deliveryStates, type AuditDeliveryJob, type AuditDeliveryStatus, type DeliveryCommand } from "./types";

function useAuditDelivery(accessToken: string, actorId: string | null) {
  const [filters, setFilters] = useQueryStates(deliveryParsers);
  const client = useQueryClient();
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const [accessLost, setAccessLost] = useState(false);
  const statusOptions = {
    queryKey: ["retail", "audit-delivery", actorId, "status"],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<AuditDeliveryStatus>("/admin/audit-delivery", accessToken, { signal }),
    retry: false,
    refetchInterval: (query: Query<AuditDeliveryStatus>): number | false => (query.state.error ? false : 10_000),
  };
  const status = useQuery(statusOptions);
  const listOptions = {
    queryKey: ["retail", "audit-delivery", actorId, "deliveries", filters],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<AuditDeliveryJob>>(
        managementQuery("/admin/audit-delivery/deliveries", {
          state: filters.delivery_state,
          limit: 25,
          offset: (filters.delivery_page - 1) * 25,
        }),
        accessToken,
        { signal },
      ),
    retry: false,
    refetchInterval: (query: Query<PageResult<AuditDeliveryJob>>): number | false =>
      query.state.error ? false : 10_000,
  };
  const deliveries = useQuery(listOptions);
  const mutationOptions = {
    mutationFn: (command: DeliveryCommand) => {
      const request = deliveryRequest(command);
      return retailRequest<unknown>(request.path, accessToken, {
        method: request.method,
        headers: { "Idempotency-Key": managementAttempt(attempt, command) },
        body: JSON.stringify(request.body),
      });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail", "audit-delivery"] });
      void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
    },
    onError: (error: Error) => {
      if (deliveryAccessLost(error)) setAccessLost(true);
    },
  };
  const mutation = useMutation(mutationOptions);
  const reload = async () => {
    const results = await Promise.all([status.refetch(), deliveries.refetch()]);
    if (results.every((result) => !result.error)) {
      setAccessLost(false);
      mutation.reset();
    }
  };
  return { filters, setFilters, status, deliveries, mutation, accessLost, reload };
}

function AuditDeliveryWorkspace({ accessToken, actorId }: { accessToken: string; actorId: string | null }) {
  const id = useId();
  const { filters, setFilters, status, deliveries, mutation, accessLost, reload } = useAuditDelivery(
    accessToken,
    actorId,
  );
  const error = status.error ?? deliveries.error;
  const data = status.data && deliveries.data ? { status: status.data, deliveries: deliveries.data } : null;
  const visible = error || accessLost ? null : data;
  const fetching = status.isFetching || deliveries.isFetching;
  const conflict = managementConflict(mutation.error);
  const busy = mutation.isPending || conflict || fetching;
  const command = (value: DeliveryCommand) => mutation.mutate(value);
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Send />
            Audit delivery
          </PageHeaderTitle>
          <PageHeaderDescription>
            Send supported management-event metadata to an operator-configured receiver.
          </PageHeaderDescription>
        </PageHeader>
        <div className="flex flex-wrap gap-2">
          <Link className={buttonVariants({ variant: "outline" })} href={uiHref("operation-audit")}>
            <ArrowLeft />
            Operation audit
          </Link>
          <Button variant="outline" onClick={() => void reload()} disabled={fetching || mutation.isPending}>
            <RefreshCw />
            Reload deliveries
          </Button>
        </div>
      </div>
      {(status.isPending || deliveries.isPending) && (
        <LoadingState
          title="Loading audit delivery"
          description="Checking the current configuration and retained delivery records."
        />
      )}
      {(error || accessLost) && (
        <StateMessage
          role="alert"
          icon={<Send className="size-5" />}
          title="Audit delivery is unavailable"
          description={
            error ? deliveryRequestError(error) : "Administrator access changed. Reload to check your access again."
          }
        />
      )}
      {visible && (
        <>
          {mutation.error && (
            <Alert variant="error">
              <AlertTitle>{conflict ? "Delivery needs review" : "Unable to update delivery"}</AlertTitle>
              <AlertDescription>{deliveryRequestError(mutation.error)}</AlertDescription>
            </Alert>
          )}
          <DeliveryStatus status={visible.status} busy={busy} onCommand={command} />
          <section className="space-y-3" aria-label="Retained audit deliveries">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <h2 className="text-base font-semibold">Retained deliveries</h2>
              <div className="space-y-2">
                <Label htmlFor={id}>Delivery status</Label>
                <select
                  id={id}
                  className="h-9 min-w-44 rounded-md border border-input bg-background px-3 text-sm"
                  value={filters.delivery_state}
                  disabled={mutation.isPending}
                  onChange={(event) =>
                    void setFilters({
                      delivery_state: event.target.value as typeof filters.delivery_state,
                      delivery_page: 1,
                    })
                  }
                >
                  <option value="">All statuses</option>
                  {deliveryStates.map((state) => (
                    <option key={state} value={state}>
                      {deliveryStateLabels[state]}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="overflow-hidden rounded-xl border">
              {visible.deliveries.items.length ? (
                <DeliveryTable
                  rows={visible.deliveries.items}
                  status={visible.status}
                  busy={busy}
                  onRetry={(job) => command({ kind: "retry", id: job.id, expected_version: job.version })}
                />
              ) : (
                <StateMessage
                  role="status"
                  icon={<Send className="size-5" />}
                  title="No matching retained deliveries"
                  description="Supported events appear after delivery is enabled. Try another status if records were filtered out."
                />
              )}
              <ManagementPagination
                page={filters.delivery_page}
                total={visible.deliveries.total}
                loading={fetching || mutation.isPending}
                onChange={(page) => void setFilters({ delivery_page: page })}
              />
            </div>
          </section>
        </>
      )}
    </>
  );
}

export default function RetailAuditDeliveryPanel() {
  const { userRole, isViewOnly, isAuthorized, isLoading, accessToken, userId } = useAuthorized();
  if (!isLoading && !isAuthorized) return null;
  if (isLoading)
    return (
      <Page>
        <LoadingState title="Loading audit delivery" description="Checking administrator access." />
      </Page>
    );
  if (!accessToken || !canUseAdminControlPlane({ userRole, isViewOnly })) {
    return (
      <Page>
        <StateMessage
          role="alert"
          icon={<Send className="size-5" />}
          title="Gateway administrator access required"
          description="Audit delivery is managed by writable gateway administrators."
        />
      </Page>
    );
  }
  return (
    <Page>
      <ManagementFeatureGate feature="audit_delivery_enabled" title="Audit delivery">
        <AuditDeliveryWorkspace accessToken={accessToken} actorId={userId} />
      </ManagementFeatureGate>
    </Page>
  );
}
