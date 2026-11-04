"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { RetailRequestError, retailRequest } from "../../api";
import { managementAttempt, managementConflict, type IdempotencyAttempt } from "../../management/ManagementShared";
import type { RetailOrganization, RetailOrganizationMember } from "../../organizations/types";
import { useDelegationConfiguration } from "./DelegatedShared";
import { delegatedAccessLost, delegatedError, grantPayload } from "./input";
import type { FinanceGrant, GrantUpdate } from "./types";

function grantChoice(grant: FinanceGrant): string {
  if (grant.can_export) return "export";
  return grant.can_view ? "view" : "none";
}
function grantMessage(grant: FinanceGrant): string {
  const labels: Record<FinanceGrant["state"], string> = {
    unassigned: "No finance access granted",
    revoked: "Finance access cleared",
    active: "Finance access active",
    organization_disabled: "Organization disabled — access inactive",
    membership_inactive: "Member ineligible — access inactive",
    membership_changed: "Membership changed — approval must be renewed",
    organization_changed: "Organization access changed — approval must be renewed",
  };
  return labels[grant.state];
}
function grantFailure(error: Error): string {
  if (error instanceof RetailRequestError && error.status === 422)
    return "This member is no longer eligible. Reload members and finance access before trying again.";
  if (managementConflict(error)) return "Finance access changed. Reload before saving again.";
  return delegatedError(error);
}

function grantEditor({
  grant,
  selection,
  member,
  organization,
  error,
  pending,
}: {
  grant: FinanceGrant | undefined;
  selection: string | null;
  member: RetailOrganizationMember;
  organization: RetailOrganization;
  error: Error | null;
  pending: boolean;
}) {
  const blocked = managementConflict(error) || pending;
  if (!grant) return { value: "none", blocked, canApprove: false, renewal: false, maySave: false };
  const value = selection ?? grantChoice(grant);
  const memberEligible = member.role === "admin" && member.enabled && organization.enabled;
  const serverEligible = grant.state !== "membership_inactive" && grant.state !== "organization_disabled";
  const canApprove = memberEligible && serverEligible;
  const renewal = grant.can_view && !grant.effective && canApprove;
  const changed = value !== grantChoice(grant);
  const eligibleSelection = value === "none" || canApprove;
  const needsApproval = changed || renewal;
  return { value, blocked, canApprove, renewal, maySave: eligibleSelection && needsApproval && !blocked };
}

function GrantWorkspace({
  organization,
  member,
  accessToken,
  actorId,
  onSaving,
  onSaved,
}: {
  organization: RetailOrganization;
  member: RetailOrganizationMember;
  accessToken: string;
  actorId: string | null;
  onSaving: (saving: boolean) => void;
  onSaved: () => void;
}) {
  const [selection, setSelection] = useState<string | null>(null);
  const [lost, setLost] = useState(false);
  const [saved, setSaved] = useState(false);
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const path = `/admin/organizations/${encodeURIComponent(organization.id)}/finance-grants/${encodeURIComponent(member.user_id)}`;
  const grant = useQuery({
    queryKey: ["retail", "admin-finance-grant", actorId, organization.id, member.user_id],
    queryFn: ({ signal }) => retailRequest<FinanceGrant>(path, accessToken, { signal }),
    retry: false,
  });
  const options = {
    mutationFn: (payload: GrantUpdate) =>
      retailRequest<FinanceGrant>(path, accessToken, {
        method: "PATCH",
        body: JSON.stringify(payload),
        headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
      }),
    onMutate: () => onSaving(true),
    onSettled: () => onSaving(false),
    onSuccess: () => {
      setSaved(true);
      onSaved();
      void grant.refetch();
    },
    onError: (error: Error) => {
      if (delegatedAccessLost(error)) setLost(true);
    },
  };
  const save = useMutation(options);
  const visible = !grant.error && !lost ? grant.data : undefined;
  const pending = save.isPending || grant.isFetching;
  const editorOptions = { grant: visible, selection, member, organization, error: save.error, pending };
  const { value, canApprove, renewal, blocked, maySave } = grantEditor(editorOptions);
  const reload = async () => {
    save.reset();
    setSelection(null);
    setSaved(false);
    const result = await grant.refetch();
    if (!result.error) {
      setLost(false);
      attempt.current = null;
    }
  };
  return (
    <div className="space-y-5">
      <p className="max-w-prose text-sm text-muted-foreground">
        This grant includes the organization’s existing attributed usage history. It permits finance reading and
        optional CSV exports; it does not permit wallet, pricing, member, or gateway changes.
      </p>
      {grant.isPending && <p role="status">Loading finance access…</p>}
      {grant.error && (
        <p role="alert" className="text-sm text-destructive">
          {grantFailure(grant.error)}
        </p>
      )}
      {lost && (
        <p role="alert" className="text-sm text-destructive">
          Grant access changed. Reload to verify your current access.
        </p>
      )}
      {visible && (
        <>
          <p className="text-sm font-medium">{grantMessage(visible)}</p>
          <div className="space-y-2">
            <Label htmlFor="finance-grant-level">Finance permission</Label>
            <select
              id="finance-grant-level"
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={value}
              disabled={blocked}
              onChange={(event) => {
                setSelection(event.target.value);
                setSaved(false);
              }}
            >
              <option value="none">None</option>
              <option value="view" disabled={!canApprove}>
                View only
              </option>
              <option value="export" disabled={!canApprove}>
                View and export
              </option>
            </select>
          </div>
          {!canApprove && (
            <p className="text-sm text-muted-foreground">
              Only an enabled organization admin can receive access. Existing access can be cleared.
            </p>
          )}
          {renewal && (
            <p className="text-sm text-muted-foreground">
              Approval must be renewed explicitly. Earlier export tasks will remain unavailable after approval.
            </p>
          )}
        </>
      )}
      {save.error && (
        <p role="alert" className="text-sm text-destructive">
          {grantFailure(save.error)}
        </p>
      )}
      {saved && (
        <p role="status" className="text-sm">
          Finance access saved.
        </p>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        <Button variant="outline" disabled={pending} onClick={() => void reload()}>
          Reload finance access
        </Button>
        {visible && (
          <Button
            disabled={!maySave}
            onClick={() => {
              const payload = grantPayload(value, visible.version);
              if (payload) save.mutate(payload);
            }}
          >
            {value === "none" ? "Clear finance access" : "Approve finance access"}
          </Button>
        )}
      </div>
    </div>
  );
}

export default function FinanceGrantDialog({
  organization,
  member,
  onClose,
  onSaved,
}: {
  organization: RetailOrganization;
  member: RetailOrganizationMember;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { userRole, isViewOnly, isAuthorized, accessToken, userId } = useAuthorized();
  const { enabled } = useDelegationConfiguration(canUseAdminControlPlane({ userRole, isViewOnly }));
  const [saving, setSaving] = useState(false);
  const authorized = isAuthorized && canUseAdminControlPlane({ userRole, isViewOnly });
  if (!authorized || !accessToken || !enabled) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && !saving && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Organization finance access</DialogTitle>
          <DialogDescription>{member.user_id}</DialogDescription>
        </DialogHeader>
        <GrantWorkspace
          organization={organization}
          member={member}
          accessToken={accessToken}
          actorId={userId}
          onSaving={setSaving}
          onSaved={onSaved}
        />
      </DialogContent>
    </Dialog>
  );
}
