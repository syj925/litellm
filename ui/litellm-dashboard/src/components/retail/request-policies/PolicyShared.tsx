"use client";

import { CircleAlert } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { managementConflict } from "../management/ManagementShared";
import type { PageResult } from "../api";
import { usePrivateResource } from "./api";
import type { PolicySession } from "./types";

export const selectClass =
  "h-9 w-full min-w-0 rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
export function policyFieldId(name: string): string {
  return `policy-${name.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}`;
}
export function PolicyField({ name, children, hint }: { name: string; children: ReactNode; hint?: string }) {
  return (
    <div className="space-y-2">
      <Label htmlFor={policyFieldId(name)}>{name}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}
export function PolicyName({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  return (
    <PolicyField name="Name">
      <Input
        id={policyFieldId("Name")}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        maxLength={100}
        required
        disabled={disabled}
        autoComplete="off"
      />
    </PolicyField>
  );
}
export function PolicyWriteError({ error }: { error: Error | null }) {
  if (!error) return null;
  const conflict = managementConflict(error);
  return (
    <Alert variant="error">
      <CircleAlert />
      <AlertTitle>{conflict ? "Changes need review" : "Unable to save changes"}</AlertTitle>
      <AlertDescription>
        {conflict
          ? "The saved version changed, or this resource is still referenced. Close this editor and refresh before trying again. No automatic retry was made."
          : error.message}
      </AlertDescription>
    </Alert>
  );
}
export function PolicyChoice<T extends { id: string; name: string; enabled: boolean }>({
  path,
  session,
  value,
  onChange,
  label,
  optional = false,
  disabled = false,
}: {
  path: string;
  session: PolicySession;
  value: string;
  onChange: (value: string) => void;
  label: string;
  optional?: boolean;
  disabled?: boolean;
}) {
  const [offset, setOffset] = useState(0);
  const data = usePrivateResource<PageResult<T>>(`${path}?limit=25&offset=${offset}`, session);
  return (
    <div className="space-y-2">
      <PolicyField name={label}>
        <select
          id={policyFieldId(label)}
          className={selectClass}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={disabled || !data.data}
          required={!optional}
        >
          <option value="">{optional ? "None" : "Choose a policy"}</option>
          {value && !data.data?.items.some((item) => item.id === value) && (
            <option value={value}>Current selection</option>
          )}
          {data.data?.items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
              {item.enabled ? "" : " (paused)"}
            </option>
          ))}
        </select>
      </PolicyField>
      {data.error && (
        <p role="alert" className="text-sm text-destructive">
          {data.error.message}
        </p>
      )}
      {!data.data && !data.error && (
        <LoadingState title={`Loading ${label.toLowerCase()}`} description="Loading available choices." />
      )}
      <div className="flex gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || !offset || !data.data}
          onClick={() => setOffset(offset - 25)}
        >
          Previous choices
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || !data.data || offset + 25 >= data.data.total}
          onClick={() => setOffset(offset + 25)}
        >
          Next choices
        </Button>
      </div>
    </div>
  );
}
