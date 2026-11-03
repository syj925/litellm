"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState, type FormEvent } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
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
import { retailRequest } from "../api";
import { managementAttempt, managementConflict, type IdempotencyAttempt } from "../management/ManagementShared";
import type { RetailOrganization } from "./types";

export default function OrganizationEditor({
  accessToken,
  organization,
  onClose,
}: {
  accessToken: string;
  organization: RetailOrganization | null;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const formId = useId();
  const [name, setName] = useState(organization?.name ?? "");
  const [adminUserId, setAdminUserId] = useState("");
  const [enabled, setEnabled] = useState(organization?.enabled ?? true);
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const save = useMutation({
    mutationFn: () => {
      if (organization) {
        const payload = { name: name.trim(), enabled, expected_version: organization.version };
        return retailRequest<RetailOrganization>(
          `/admin/organizations/${encodeURIComponent(organization.id)}`,
          accessToken,
          {
            method: "PATCH",
            headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
            body: JSON.stringify(payload),
          },
        );
      }
      const payload = { name: name.trim(), admin_user_id: adminUserId.trim() };
      return retailRequest<RetailOrganization>("/admin/organizations", accessToken, {
        method: "POST",
        headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
        body: JSON.stringify(payload),
      });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail", "organizations"] });
      void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
      onClose();
    },
  });
  const conflict = managementConflict(save.error);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (conflict || save.isPending || !name.trim()) return;
    if (!organization && !adminUserId.trim()) return;
    save.mutate();
  };
  const fieldsMissing = !name.trim() || (!organization && !adminUserId.trim());
  const saveDisabled = save.isPending || conflict || fieldsMissing;
  return (
    <Dialog open onOpenChange={(open) => !open && !save.isPending && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{organization ? "Edit organization" : "Create organization"}</DialogTitle>
          <DialogDescription>
            {organization
              ? "Changes apply to this organization and its memberships."
              : "Associate an existing account as the first organization admin. Its gateway role and wallet remain unchanged."}
          </DialogDescription>
        </DialogHeader>
        <form id={formId} onSubmit={submit} className="space-y-5">
          {save.error && (
            <Alert variant="error">
              <AlertTitle>{conflict ? "Organization changed" : "Unable to save organization"}</AlertTitle>
              <AlertDescription>
                {conflict
                  ? "Close this form and refresh the list before editing again. Your changes have not been applied."
                  : save.error.message}
              </AlertDescription>
            </Alert>
          )}
          <div className="space-y-2">
            <Label htmlFor={`${formId}-name`}>Organization name</Label>
            <Input
              id={`${formId}-name`}
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={100}
              required
              disabled={save.isPending || conflict}
              autoComplete="off"
            />
          </div>
          {!organization && (
            <div className="space-y-2">
              <Label htmlFor={`${formId}-admin`}>Existing admin account ID</Label>
              <Input
                id={`${formId}-admin`}
                value={adminUserId}
                onChange={(event) => setAdminUserId(event.target.value)}
                maxLength={191}
                required
                disabled={save.isPending || conflict}
                autoComplete="off"
              />
              <p className="text-xs text-muted-foreground">
                Use the exact account ID already registered with this gateway.
              </p>
            </div>
          )}
          {organization && (
            <div className="space-y-2">
              <label className="flex items-center gap-3">
                <Checkbox
                  checked={enabled}
                  onCheckedChange={(value) => setEnabled(value === true)}
                  disabled={save.isPending || conflict}
                />
                <span className="text-sm font-medium">Organization enabled</span>
              </label>
              <p className="text-xs leading-relaxed text-muted-foreground">
                Disabling the organization removes member access to its organization features. Personal wallet balances
                are unaffected.
              </p>
            </div>
          )}
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" form={formId} disabled={saveDisabled}>
            {save.isPending ? "Saving..." : "Save organization"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
