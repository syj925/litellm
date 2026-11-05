"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { managementConflict } from "../management/ManagementShared";
import { usePolicyWrite } from "./api";
import { buildDefinition, definitionFields, policyRoutes } from "./input";
import { PolicyChoice, PolicyField, PolicyName, PolicyWriteError, policyFieldId } from "./PolicyShared";
import type { PolicyRoute, PolicySession, RequestPolicy, SavedPrompt } from "./types";

export default function PolicyEditor({
  session,
  policy,
  onClose,
  onSaved,
}: {
  session: PolicySession;
  policy: RequestPolicy | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(policy?.name ?? "");
  const [enabled, setEnabled] = useState(policy?.enabled ?? true);
  const [prompt, setPrompt] = useState(policy?.prompt_id ?? "");
  const [restricted, setRestricted] = useState(policy?.definition.allowed_routes != null);
  const [routes, setRoutes] = useState<PolicyRoute[]>(policy?.definition.allowed_routes ?? [...policyRoutes]);
  const [fields, setFields] = useState(() => definitionFields(policy?.definition));
  const [validation, setValidation] = useState<Error | null>(null);
  const write = usePolicyWrite(session, onSaved);
  const blocked = write.pending || managementConflict(write.error);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (blocked || !name.trim()) return;
    try {
      const definition = buildDefinition(fields, restricted ? routes : null);
      setValidation(null);
      const payload = {
        name: name.trim(),
        definition,
        prompt_id: prompt || null,
        enabled,
        ...(policy ? { expected_version: policy.version } : {}),
      };
      void write.run(policy ? `/policies/${policy.id}` : "/policies", policy ? "PUT" : "POST", payload);
    } catch (cause) {
      setValidation(cause instanceof Error ? cause : new Error("Check the policy fields."));
    }
  };
  return (
    <form
      onSubmit={submit}
      className="space-y-5 rounded-lg border p-5"
      aria-label={policy ? "Edit request policy" : "Create request policy"}
    >
      <h2 className="text-lg font-semibold">{policy ? "Edit request policy" : "Create request policy"}</h2>
      <PolicyWriteError error={write.error ?? validation} />
      <PolicyName value={name} onChange={setName} disabled={blocked} />
      <fieldset disabled={blocked} className="space-y-3">
        <legend className="mb-2 text-sm font-medium">Allowed API routes</legend>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={restricted} onCheckedChange={(value) => setRestricted(value === true)} />
          Restrict API routes
        </label>
        <div className="flex flex-wrap gap-5">
          {policyRoutes.map((route) => (
            <label key={route} className="flex items-center gap-2 text-sm">
              <Checkbox
                disabled={!restricted}
                checked={routes.includes(route)}
                onCheckedChange={(value) =>
                  setRoutes(value === true ? [...routes, route] : routes.filter((item) => item !== route))
                }
              />
              {route}
            </label>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Unchecked restriction allows all supported routes. A restriction with no routes blocks every request.
        </p>
      </fieldset>
      <div className="grid gap-4 sm:grid-cols-2">
        {(
          [
            { key: "output", label: "Output cap", min: 1, max: 1000000, step: "1" },
            { key: "temperature", label: "Temperature", min: 0, max: 2, step: "any" },
            { key: "topP", label: "Top P", min: 0, max: 1, step: "any" },
            { key: "seed", label: "Seed", min: Number.MIN_SAFE_INTEGER, max: Number.MAX_SAFE_INTEGER, step: "1" },
          ] as const
        ).map((field) => (
          <PolicyField key={field.key} name={field.label} hint="Leave blank to keep this parameter unrestricted.">
            <Input
              id={policyFieldId(field.label)}
              type="number"
              value={fields[field.key]}
              onChange={(event) => setFields({ ...fields, [field.key]: event.target.value })}
              min={field.min}
              max={field.max}
              step={field.step}
              disabled={blocked}
            />
          </PolicyField>
        ))}
      </div>
      <p className="max-w-prose text-sm text-muted-foreground">
        Responses output limits below 16 are rejected. Seed applies only to Chat. Messages temperature must be 1 or
        less.
      </p>
      <PolicyChoice<SavedPrompt>
        path="/prompts"
        session={session}
        value={prompt}
        onChange={setPrompt}
        label="Saved prompt"
        optional
        disabled={blocked}
      />
      <label className="flex items-center gap-2 text-sm">
        <Checkbox checked={enabled} onCheckedChange={(value) => setEnabled(value === true)} disabled={blocked} />
        Policy enabled
      </label>
      <p className="max-w-prose text-sm text-muted-foreground">
        Pausing a policy blocks requests while an applied binding still references it. Stop applying the binding to
        remove the restriction.
      </p>
      <div className="flex gap-2">
        <Button type="submit" disabled={blocked || !name.trim()}>
          {write.pending ? "Saving…" : "Save policy"}
        </Button>
        <Button type="button" variant="outline" onClick={onClose} disabled={write.pending}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
