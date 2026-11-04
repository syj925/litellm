"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Users } from "lucide-react";
import { useId, useRef, useState, type FormEvent } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { retailRequest, type PageResult } from "../api";
import {
  ManagementError,
  ManagementPagination,
  managementAttempt,
  managementConflict,
  managementPageSize,
  managementQuery,
  type IdempotencyAttempt,
} from "../management/ManagementShared";
import type { OrganizationMemberResult, RetailOrganization, RetailOrganizationMember } from "./types";
import FinanceGrantDialog from "../organization-finance/delegated/FinanceGrantDialog";
import { useDelegationConfiguration } from "../organization-finance/delegated/DelegatedShared";

type MembersResponse = PageResult<RetailOrganizationMember> & { organization: RetailOrganization };

function MemberEditor({
  organization,
  member,
  accessToken,
  onSaved,
  onClose,
}: {
  organization: RetailOrganization;
  member: RetailOrganizationMember | null;
  accessToken: string;
  onSaved: (organization: RetailOrganization) => void;
  onClose: () => void;
}) {
  const formId = useId();
  const initial = member ?? { user_id: "", role: "member" as const, enabled: true };
  const [userId, setUserId] = useState(initial.user_id);
  const [role, setRole] = useState<RetailOrganizationMember["role"]>(initial.role);
  const [enabled, setEnabled] = useState(initial.enabled);
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const save = useMutation({
    mutationFn: () => {
      const path = `/organizations/${encodeURIComponent(organization.id)}/members`;
      if (member) {
        const payload = {
          role,
          enabled,
          expected_version: member.version,
          expected_organization_version: organization.version,
        };
        return retailRequest<OrganizationMemberResult>(`${path}/${encodeURIComponent(member.user_id)}`, accessToken, {
          method: "PATCH",
          headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
          body: JSON.stringify(payload),
        });
      }
      const payload = { user_id: userId.trim(), role, expected_organization_version: organization.version };
      return retailRequest<OrganizationMemberResult>(path, accessToken, {
        method: "POST",
        headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
        body: JSON.stringify(payload),
      });
    },
    onSuccess: (result) => onSaved(result.organization),
  });
  const conflict = managementConflict(save.error);
  const closeBlocked = save.isPending;
  const mayChangeRole = organization.can_associate_members;
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!userId.trim() || save.isPending || conflict) return;
    save.mutate();
  };
  return (
    <Dialog open onOpenChange={(open) => !open && !closeBlocked && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{member ? "Edit member" : "Associate existing account"}</DialogTitle>
          <DialogDescription>
            {organization.can_associate_members
              ? "Organization roles only apply inside this organization. They do not grant gateway administrator access."
              : "You can enable or disable previously approved ordinary members. Ask a gateway administrator to associate accounts or change administrator roles."}
          </DialogDescription>
        </DialogHeader>
        <form id={formId} onSubmit={submit} className="space-y-5">
          {save.error && (
            <Alert variant="error">
              <AlertTitle>{conflict ? "Membership changed" : "Unable to save member"}</AlertTitle>
              <AlertDescription>
                {conflict
                  ? "Close this form and refresh the members before trying again. Your changes have not been applied."
                  : save.error.message}
              </AlertDescription>
            </Alert>
          )}
          <div className="space-y-2">
            <Label htmlFor={`${formId}-account`}>Account ID</Label>
            <Input
              id={`${formId}-account`}
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
              disabled={Boolean(member) || save.isPending || conflict}
              required
              maxLength={191}
              autoComplete="off"
            />
          </div>
          {mayChangeRole ? (
            <div className="space-y-2">
              <Label htmlFor={`${formId}-role`}>Organization role</Label>
              <select
                id={`${formId}-role`}
                value={role}
                onChange={(event) => setRole(event.target.value === "admin" ? "admin" : "member")}
                disabled={save.isPending || conflict}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              >
                <option value="member">Member</option>
                <option value="admin">Organization admin</option>
              </select>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Role: Member</p>
          )}
          {member && (
            <label className="flex items-center gap-3">
              <Checkbox
                checked={enabled}
                onCheckedChange={(value) => setEnabled(value === true)}
                disabled={save.isPending || conflict}
              />
              <span className="text-sm font-medium">Membership enabled</span>
            </label>
          )}
        </form>
        <DialogFooter>
          <Button variant="outline" disabled={save.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form={formId} disabled={save.isPending || conflict || !userId.trim()}>
            {save.isPending ? "Saving..." : "Save member"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function OrganizationMembers({
  organization,
  accessToken,
  actorId,
  onClose,
  onOrganizationChange,
}: {
  organization: RetailOrganization;
  accessToken: string;
  actorId: string | null;
  onClose: () => void;
  onOrganizationChange: (organization: RetailOrganization) => void;
}) {
  const client = useQueryClient();
  const { userRole, isViewOnly } = useAuthorized();
  const delegation = useDelegationConfiguration(canUseAdminControlPlane({ userRole, isViewOnly }));
  const canManageFinance = canUseAdminControlPlane({ userRole, isViewOnly }) && delegation.enabled;
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<{ member: RetailOrganizationMember | null } | null>(null);
  const [financeMember, setFinanceMember] = useState<RetailOrganizationMember | null>(null);
  const members = useQuery({
    queryKey: ["retail", "organization-members", actorId, organization.id, page],
    queryFn: ({ signal }) =>
      retailRequest<MembersResponse>(
        managementQuery(`/organizations/${encodeURIComponent(organization.id)}/members`, {
          limit: managementPageSize,
          offset: (page - 1) * managementPageSize,
        }),
        accessToken,
        { signal },
      ),
    retry: false,
  });
  const current = members.data?.organization ?? organization;
  const mayEditMember = (member: RetailOrganizationMember) =>
    current.can_manage_members && (current.can_associate_members || member.role === "member");
  const mayManageFinance = (member: RetailOrganizationMember) =>
    canManageFinance && (member.role === "admin" || member.has_finance_grant);
  const editorOpen = Boolean(editing) || Boolean(financeMember);
  const showFinance = !members.error && canManageFinance;
  const saved = (updated: RetailOrganization) => {
    setEditing(null);
    onOrganizationChange(updated);
    void client.invalidateQueries({ queryKey: ["retail", "organizations"] });
    void client.invalidateQueries({ queryKey: ["retail", "organization-members"] });
    void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
  };
  return (
    <>
      <Dialog open onOpenChange={(open) => !open && !editorOpen && onClose()}>
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>{members.error ? "Organization members" : `${current.name} members`}</DialogTitle>
            <DialogDescription>
              Membership controls access to organization features. It does not share personal wallet balances or grant
              model access.
            </DialogDescription>
          </DialogHeader>
          {members.isPending && (
            <LoadingState description="Loading authorized gateway data." title="Loading organization members" />
          )}
          {members.error && <ManagementError title="Unable to load members" error={members.error} />}
          {members.data && !members.error && (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-muted-foreground">Organization version {current.version}</p>
                {current.can_associate_members && (
                  <Button size="sm" onClick={() => setEditing({ member: null })}>
                    <Plus />
                    Associate account
                  </Button>
                )}
              </div>
              {!current.can_manage_members && (
                <p className="text-sm text-muted-foreground">Your membership provides read-only access to this list.</p>
              )}
              {members.data.items.length === 0 ? (
                <StateMessage
                  role="status"
                  icon={<Users className="size-5" />}
                  title="No members"
                  description="A gateway administrator can associate existing accounts with this organization."
                />
              ) : (
                <div className="overflow-hidden rounded-lg border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Account ID</TableHead>
                        <TableHead>Role</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>
                          <span className="sr-only">Actions</span>
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {members.data.items.map((member) => (
                        <TableRow key={member.user_id}>
                          <TableCell className="max-w-64 break-all font-mono text-xs">{member.user_id}</TableCell>
                          <TableCell>{member.role === "admin" ? "Organization admin" : "Member"}</TableCell>
                          <TableCell>
                            <Badge variant={member.enabled ? "secondary" : "outline"}>
                              {member.enabled ? "Enabled" : "Disabled"}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-right">
                            {mayManageFinance(member) && (
                              <Button
                                variant="outline"
                                size="sm"
                                className="mr-2"
                                aria-label={`Finance access for ${member.user_id}`}
                                onClick={() => setFinanceMember(member)}
                              >
                                Finance access
                              </Button>
                            )}
                            {mayEditMember(member) && (
                              <Button
                                variant="outline"
                                size="sm"
                                aria-label={`Edit member ${member.user_id}`}
                                onClick={() => setEditing({ member })}
                              >
                                Edit
                              </Button>
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                  <ManagementPagination
                    page={page}
                    total={members.data.total}
                    loading={members.isFetching}
                    onChange={setPage}
                  />
                </div>
              )}
              <Button variant="outline" onClick={() => void members.refetch()} disabled={members.isFetching}>
                Refresh members
              </Button>
            </>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {editing && !members.error && (
        <MemberEditor
          accessToken={accessToken}
          organization={current}
          member={editing.member}
          onClose={() => setEditing(null)}
          onSaved={saved}
        />
      )}
      {financeMember && showFinance && (
        <FinanceGrantDialog
          organization={current}
          member={members.data?.items.find((member) => member.user_id === financeMember.user_id) ?? financeMember}
          onClose={() => setFinanceMember(null)}
          onSaved={() => {
            void client.invalidateQueries({ queryKey: ["retail", "organization-members"] });
            void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
          }}
        />
      )}
    </>
  );
}
