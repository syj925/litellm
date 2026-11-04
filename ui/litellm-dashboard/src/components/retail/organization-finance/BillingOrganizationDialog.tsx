"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useQueryStates } from "nuqs";
import { useCallback, useRef, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { retailRequest } from "../api";
import {
  ManagementFeatureGate,
  managementAttempt,
  managementConflict,
  managementTimestamp,
  type IdempotencyAttempt,
} from "../management/ManagementShared";
import { assignmentLabel, assignmentPickerParsers, billingPayload, financeAccessLost } from "./financeInput";
import { FinanceError } from "./FinanceShared";
import OrganizationPicker from "./OrganizationPicker";
import type { BillingOrganization } from "./types";

function CurrentAssignment({ assignment }: { assignment: BillingOrganization }) {
  return (
    <section className="space-y-2 rounded-lg border p-4" aria-label="Current billing organization">
      <h3 className="text-sm font-medium">Current assignment</h3>
      <p className="break-words font-medium">{assignment.organization_name ?? "No organization"}</p>
      <p className="text-sm text-muted-foreground">{assignmentLabel(assignment.state)}</p>
      {assignment.organization_id && (
        <p className="break-all font-mono text-xs text-muted-foreground">{assignment.organization_id}</p>
      )}
      {assignment.updated_at && (
        <p className="text-xs text-muted-foreground">Updated {managementTimestamp(assignment.updated_at)} (Beijing)</p>
      )}
    </section>
  );
}

function AssignmentWorkspace({
  userId,
  accessToken,
  actorId,
  onSavingChange,
}: {
  userId: string;
  accessToken: string;
  actorId: string | null;
  onSavingChange: (value: boolean) => void;
}) {
  const client = useQueryClient();
  const [selection, setSelection] = useState("");
  const [ready, setReady] = useState(false);
  const [authorityLost, setAuthorityLost] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [saved, setSaved] = useState(false);
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const path = `/admin/users/${encodeURIComponent(userId)}/billing-organization`;
  const queryKey = ["retail", "organization-finance", actorId, "assignment", userId];
  const assignment = useQuery({
    queryKey,
    queryFn: ({ signal }) => retailRequest<BillingOrganization>(path, accessToken, { signal }),
    retry: false,
  });
  const unavailable = useCallback(() => setAuthorityLost(true), []);
  const mutationOptions = {
    mutationFn: (payload: { organization_id: string | null; expected_version: number }) =>
      retailRequest<BillingOrganization>(path, accessToken, {
        method: "PATCH",
        body: JSON.stringify(payload),
        headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
      }),
    onMutate: () => onSavingChange(true),
    onSettled: () => onSavingChange(false),
    onError: (error: Error) => {
      if (financeAccessLost(error, true)) unavailable();
    },
    onSuccess: (result: BillingOrganization) => {
      client.setQueryData(queryKey, result);
      setSelection("");
      setSaved(true);
      attempt.current = null;
      void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
    },
  };
  const mutation = useMutation(mutationOptions);
  const visible = !assignment.error && !authorityLost ? assignment.data : undefined;
  const conflict = managementConflict(mutation.error);
  const reload = async () => {
    mutation.reset();
    setSelection("");
    setSaved(false);
    const result = await assignment.refetch();
    if (!result.error) {
      setAuthorityLost(false);
      attempt.current = null;
      setRefreshKey((key) => key + 1);
    }
  };
  const payload = visible ? billingPayload(selection, visible.version) : null;
  const busy = mutation.isPending || assignment.isFetching;
  const missingSelection = !payload || !ready;
  const cannotSave = missingSelection || conflict || busy;
  return (
    <div className="space-y-5">
      <p className="max-w-prose text-sm text-muted-foreground">
        Applies to requests accepted after this change. Earlier requests keep their original organization.
      </p>
      {assignment.isPending && <p role="status">Loading current assignment…</p>}
      {assignment.error && <FinanceError error={assignment.error} context="assignment" />}
      {authorityLost && !assignment.error && (
        <p role="alert" className="text-sm text-destructive">
          Assignment access changed. Reload to check your current access.
        </p>
      )}
      {visible && (
        <>
          <CurrentAssignment assignment={visible} />
          <OrganizationPicker
            key={refreshKey}
            refreshKey={refreshKey}
            accessToken={accessToken}
            actorId={actorId}
            userId={userId}
            value={selection}
            disabled={busy || conflict}
            onSelect={(value) => {
              setSelection(value);
              setSaved(false);
            }}
            onReady={setReady}
            onUnavailable={unavailable}
          />
          <details className="text-sm text-muted-foreground">
            <summary className="cursor-pointer">How assignment works</summary>
            <p className="mt-2 max-w-prose">
              Choose one active membership explicitly. An inactive assignment stays visible for history. Membership
              changes do not move earlier charges or freeze the customer’s personal wallet.
            </p>
          </details>
        </>
      )}
      {mutation.error && <FinanceError error={mutation.error} context="assignment" />}
      {saved && (
        <p role="status" className="text-sm">
          Billing organization saved for future requests.
        </p>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        <Button variant="outline" disabled={mutation.isPending || assignment.isFetching} onClick={() => void reload()}>
          Reload assignment
        </Button>
        {visible && (
          <Button disabled={cannotSave} onClick={() => payload && mutation.mutate(payload)}>
            {mutation.isPending ? "Saving…" : "Save assignment"}
          </Button>
        )}
      </div>
    </div>
  );
}

export default function BillingOrganizationDialog({ userId, onClose }: { userId: string; onClose: () => void }) {
  const { accessToken, userId: actorId, userRole, isViewOnly, isAuthorized } = useAuthorized();
  const [saving, setSaving] = useState(false);
  const [, setFilters] = useQueryStates(assignmentPickerParsers);
  if (!isAuthorized || !accessToken || !canUseAdminControlPlane({ userRole, isViewOnly })) return null;
  const close = () => {
    if (saving) return;
    void setFilters({ billing_org_q: null, billing_org_page: null });
    onClose();
  };
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Billing organization</DialogTitle>
          <DialogDescription className="break-all">{userId}</DialogDescription>
        </DialogHeader>
        <ManagementFeatureGate feature="org_finance_enabled" title="Organization finance">
          <AssignmentWorkspace userId={userId} accessToken={accessToken} actorId={actorId} onSavingChange={setSaving} />
        </ManagementFeatureGate>
      </DialogContent>
    </Dialog>
  );
}
