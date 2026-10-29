"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronLeft, ChevronRight, CircleAlert, CreditCard, RefreshCw } from "lucide-react";
import { parseAsInteger, useQueryState } from "nuqs";
import { FormEvent, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import CopyButton from "@/components/shared/CopyButton";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "@/lib/toast";
import { formatMoney, PageResult, retailRequest } from "./api";

interface PendingReservation {
  request_id: string;
  user_id: string;
  model: string;
  reserved_amount: string;
  created_at: string;
  price_snapshot: Record<string, unknown>;
  details: Record<string, unknown>;
}

const pageSize = 25;

export default function PendingReservations({
  accessToken,
  enabled,
}: {
  accessToken: string | null;
  enabled: boolean;
}) {
  const client = useQueryClient();
  const [pendingPageValue, setPendingPage] = useQueryState("pending_page", parseAsInteger.withDefault(1));
  const pendingPage = Math.max(1, pendingPageValue);
  const [resolution, setResolution] = useState<{
    reservation: PendingReservation;
    action: "release" | "settle";
  } | null>(null);
  const [resolutionForm, setResolutionForm] = useState({ amount: "", reference: "", reason: "" });
  const [resolutionError, setResolutionError] = useState<string | null>(null);
  const [resolutionAttempt, setResolutionAttempt] = useState<{ body: string; key: string; requestId: string } | null>(
    null,
  );
  const reservations = useQuery({
    queryKey: ["retail", "reservations", pendingPage],
    queryFn: () =>
      retailRequest<PageResult<PendingReservation>>(
        `/admin/reservations?state=reserved&limit=${pageSize}&offset=${(pendingPage - 1) * pageSize}`,
        accessToken,
      ),
    enabled: enabled && Boolean(accessToken),
  });

  const resolutionMutation = useMutation({
    mutationFn: ({ body, key, requestId }: { body: string; key: string; requestId: string }) =>
      retailRequest<unknown>(`/admin/reservations/${encodeURIComponent(requestId)}/resolve`, accessToken, {
        method: "POST",
        body,
        headers: { "Idempotency-Key": key },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setResolution(null);
      setResolutionAttempt(null);
      toast.success("Reservation resolved successfully");
    },
  });

  const openResolution = (reservation: PendingReservation, action: "release" | "settle") => {
    setResolution({ reservation, action });
    setResolutionForm({ amount: "", reference: "", reason: "" });
    setResolutionError(null);
    resolutionMutation.reset();
  };

  const submitResolution = (event: FormEvent) => {
    event.preventDefault();
    if (!resolution) return;
    if (resolution.action === "settle" && !/^\d{1,9}(?:\.\d{1,9})?$/.test(resolutionForm.amount)) {
      setResolutionError(
        "Enter the verified final customer charge in USD. Zero is allowed for a confirmed free request.",
      );
      return;
    }
    if (!resolutionForm.reference.trim() || !resolutionForm.reason.trim()) {
      setResolutionError("An evidence reference and a reason are required.");
      return;
    }
    setResolutionError(null);
    const body = JSON.stringify({
      action: resolution.action,
      ...(resolution.action === "settle" ? { amount: resolutionForm.amount } : {}),
      reference: resolutionForm.reference.trim(),
      reason: resolutionForm.reason.trim(),
    });
    const requestId = resolution.reservation.request_id;
    const currentAttempt =
      resolutionAttempt?.body === body && resolutionAttempt.requestId === requestId
        ? resolutionAttempt
        : { body, key: crypto.randomUUID(), requestId };
    setResolutionAttempt(currentAttempt);
    resolutionMutation.mutate(currentAttempt);
  };

  return (
    <>
      <details className="group overflow-hidden rounded-xl border">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-5 py-4 text-sm font-medium">
          <span>
            Pending reservations{" "}
            <Badge variant="secondary" className="ml-2">
              {reservations.data?.total ?? "..."}
            </Badge>
          </span>
          <ChevronDown className="size-4 text-muted-foreground transition-transform group-open:rotate-180" />
        </summary>
        <div className="border-t">
          <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
            <p className="max-w-3xl text-xs leading-relaxed text-muted-foreground">
              Requests without a confirmed final result keep their reserved funds. Verify upstream completion and
              payment evidence before manually settling or releasing a reservation.
            </p>
            <Button
              variant="outline"
              size="sm"
              disabled={reservations.isFetching}
              onClick={() => void reservations.refetch()}
            >
              <RefreshCw />
              Refresh
            </Button>
          </div>
          {reservations.isPending ? (
            <LoadingState title="Loading reservations" description="Retrieving requests awaiting final settlement." />
          ) : reservations.error ? (
            <StateMessage
              role="alert"
              tone="destructive"
              icon={<CircleAlert className="size-5" />}
              title="Unable to load reservations"
              description={reservations.error.message}
            />
          ) : reservations.data?.items.length === 0 ? (
            <StateMessage
              role="status"
              icon={<CreditCard className="size-5" />}
              title="No pending reservations"
              description="Requests with funds awaiting settlement will appear here."
            />
          ) : (
            <Table>
              <TableHeader className="bg-muted/40">
                <TableRow>
                  <TableHead className="pl-5">Request / Account</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="text-right">Reserved (USD)</TableHead>
                  <TableHead className="pr-5 text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {reservations.data?.items.map((reservation) => (
                  <TableRow key={reservation.request_id}>
                    <TableCell className="pl-5">
                      <div className="flex items-center gap-1">
                        <span className="max-w-56 truncate font-mono text-xs" title={reservation.request_id}>
                          {reservation.request_id}
                        </span>
                        <CopyButton value={reservation.request_id} label="Copy pending request ID" />
                      </div>
                      <p className="mt-1 max-w-56 truncate text-xs text-muted-foreground" title={reservation.user_id}>
                        {reservation.user_id}
                      </p>
                    </TableCell>
                    <TableCell>{reservation.model}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {new Date(reservation.created_at).toLocaleString("en-US")}
                    </TableCell>
                    <TableCell className="text-right font-medium tabular-nums">
                      {formatMoney(reservation.reserved_amount)}
                    </TableCell>
                    <TableCell className="pr-5">
                      <div className="flex justify-end gap-2">
                        <Button variant="outline" size="xs" onClick={() => openResolution(reservation, "release")}>
                          Release
                        </Button>
                        <Button variant="outline" size="xs" onClick={() => openResolution(reservation, "settle")}>
                          Settle
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <div className="flex items-center justify-end gap-3 border-t px-5 py-3 text-xs text-muted-foreground">
            <span>
              Page {pendingPage} of {Math.max(1, Math.ceil((reservations.data?.total ?? 0) / pageSize))}
            </span>
            <Button
              size="icon-xs"
              variant="outline"
              aria-label="Previous reservations page"
              disabled={pendingPage <= 1 || reservations.isFetching}
              onClick={() => void setPendingPage(pendingPage - 1)}
            >
              <ChevronLeft />
            </Button>
            <Button
              size="icon-xs"
              variant="outline"
              aria-label="Next reservations page"
              disabled={pendingPage * pageSize >= (reservations.data?.total ?? 0) || reservations.isFetching}
              onClick={() => void setPendingPage(pendingPage + 1)}
            >
              <ChevronRight />
            </Button>
          </div>
        </div>
      </details>
      <Dialog
        open={resolution !== null}
        onOpenChange={(open) => {
          if (!open && !resolutionMutation.isPending) setResolution(null);
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {resolution?.action === "release" ? "Release reserved funds" : "Settle customer charge"}
            </DialogTitle>
            <DialogDescription>Only confirm after verifying the final upstream result.</DialogDescription>
          </DialogHeader>
          <form onSubmit={submitResolution} className="flex flex-col gap-5">
            <div className="rounded-lg border bg-muted/25 p-3 text-xs">
              <p className="break-all font-mono">{resolution?.reservation.request_id}</p>
              <p className="mt-2 text-muted-foreground">
                Reserved: {formatMoney(resolution?.reservation.reserved_amount ?? "0")} USD
              </p>
            </div>
            <FieldGroup>
              {resolution?.action === "settle" && (
                <Field>
                  <FieldLabel htmlFor="resolution-amount">Final customer charge (USD)</FieldLabel>
                  <Input
                    id="resolution-amount"
                    inputMode="decimal"
                    value={resolutionForm.amount}
                    onChange={(event) => setResolutionForm({ ...resolutionForm, amount: event.target.value })}
                    required
                    disabled={resolutionMutation.isPending}
                    placeholder="Verified actual charge"
                  />
                  <p className="text-xs text-muted-foreground">
                    Enter the customer charge after pricing, not the provider cost.
                  </p>
                </Field>
              )}
              <Field>
                <FieldLabel htmlFor="resolution-reference">Evidence reference</FieldLabel>
                <Input
                  id="resolution-reference"
                  value={resolutionForm.reference}
                  onChange={(event) => setResolutionForm({ ...resolutionForm, reference: event.target.value })}
                  required
                  maxLength={128}
                  disabled={resolutionMutation.isPending}
                  placeholder="Upstream log or review reference"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="resolution-reason">Reason</FieldLabel>
                <Input
                  id="resolution-reason"
                  value={resolutionForm.reason}
                  onChange={(event) => setResolutionForm({ ...resolutionForm, reason: event.target.value })}
                  required
                  maxLength={500}
                  disabled={resolutionMutation.isPending}
                />
              </Field>
            </FieldGroup>
            {(resolutionError || resolutionMutation.error) && (
              <Alert variant="error">
                <CircleAlert />
                <AlertTitle>Resolution was not confirmed</AlertTitle>
                <AlertDescription>{resolutionError || resolutionMutation.error?.message}</AlertDescription>
              </Alert>
            )}
            <p className="text-xs leading-relaxed text-muted-foreground">
              {resolution?.action === "release"
                ? "Releasing returns the entire reservation to the available balance without a customer charge."
                : "Settlement records the final customer charge and releases the remaining reservation."}{" "}
              This action is recorded with your administrator identity.
            </p>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={resolutionMutation.isPending}
                onClick={() => setResolution(null)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={resolutionMutation.isPending}>
                {resolutionMutation.isPending
                  ? "Confirming..."
                  : resolution?.action === "release"
                    ? "Confirm release"
                    : "Confirm settlement"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
