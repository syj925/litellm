"use client";

import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import type { PageResult } from "../api";
import {
  ManagementError,
  ManagementPagination,
  managementConflict,
  managementQuery,
} from "../management/ManagementShared";
import { LoadingState } from "@/components/shared/LoadingState";
import { usePolicyWrite, usePrivateResource } from "./api";
import { PolicyChoice, PolicyField, PolicyWriteError, selectClass, policyFieldId } from "./PolicyShared";
import type { PolicyBinding, PolicySession, PolicyTarget, RequestPolicy, TargetKind } from "./types";
import { targetKindLabels } from "./types";

function TargetPicker({
  session,
  value,
  onChange,
  disabled,
}: {
  session: PolicySession;
  value: PolicyTarget | null;
  onChange: (target: PolicyTarget | null) => void;
  disabled: boolean;
}) {
  const [kind, setKind] = useState<TargetKind>("user");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const queryParameters = { kind, q: search, limit: 25, offset: (page - 1) * 25 };
  const path = managementQuery("/targets", queryParameters);
  const targets = usePrivateResource<PageResult<PolicyTarget>>(path, session);
  const changeKind = (value: string) => {
    setKind(value as TargetKind);
    setPage(1);
    onChange(null);
  };
  return (
    <fieldset disabled={disabled} className="space-y-3">
      <legend className="mb-2 text-sm font-medium">Binding target</legend>
      <div className="grid gap-3 sm:grid-cols-[140px_1fr_auto]">
        <PolicyField name="Target type">
          <select
            id={policyFieldId("Target type")}
            className={selectClass}
            value={kind}
            onChange={(event) => changeKind(event.target.value)}
          >
            <option value="user">User</option>
            <option value="key">Virtual key</option>
            <option value="model">Public model</option>
            <option value="team">Team</option>
          </select>
        </PolicyField>
        <PolicyField name="Search targets">
          <Input
            id={policyFieldId("Search targets")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            maxLength={100}
            autoComplete="off"
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                setSearch(query.trim());
                setPage(1);
              }
            }}
          />
        </PolicyField>
        <Button
          className="self-end"
          type="button"
          variant="outline"
          onClick={() => {
            setSearch(query.trim());
            setPage(1);
          }}
        >
          Search
        </Button>
      </div>
      {value && (
        <p className="text-sm">
          Selected target: <strong>{value.label}</strong>
        </p>
      )}
      {targets.error && <ManagementError title="Unable to load targets" error={targets.error} />}
      {!targets.error && !targets.data && (
        <LoadingState title="Loading targets" description="Searching available accounts, keys, or public models." />
      )}
      {!targets.error && targets.data && (
        <div className="rounded-lg border">
          <div role="group" aria-label="Available targets" className="max-h-64 overflow-y-auto divide-y">
            {targets.data.items.length === 0 && (
              <p className="p-4 text-sm text-muted-foreground">No matching targets. Try another search.</p>
            )}
            {targets.data.items.map((target) => (
              <label
                key={target.id}
                className="flex cursor-pointer items-center gap-3 px-4 py-3 text-sm hover:bg-muted/50"
              >
                <input
                  type="radio"
                  name="binding-target"
                  checked={value?.id === target.id}
                  onChange={() => onChange(target)}
                  className="accent-primary"
                />
                <span className="min-w-0 break-words">{target.label}</span>
              </label>
            ))}
          </div>
          <ManagementPagination page={page} total={targets.data.total} loading={false} onChange={setPage} />
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Targets use gateway-managed references. Credentials and native identifiers are never shown here.
      </p>
    </fieldset>
  );
}
export default function BindingEditor({
  session,
  binding,
  onClose,
  onSaved,
}: {
  session: PolicySession;
  binding: PolicyBinding | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [target, setTarget] = useState<PolicyTarget | null>(null);
  const [policy, setPolicy] = useState(binding?.policy_id ?? "");
  const [enabled, setEnabled] = useState(binding?.enabled ?? true);
  const write = usePolicyWrite(session, onSaved);
  const blocked = write.pending || managementConflict(write.error);
  const missingTarget = !binding && !target;
  const saveDisabled = blocked || !policy || missingTarget;
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saveDisabled) return;
    const payload = binding
      ? { policy_id: policy, enabled, expected_version: binding.version }
      : { target_id: target?.id, policy_id: policy, enabled };
    void write.run(binding ? `/bindings/${binding.id}` : "/bindings", binding ? "PUT" : "POST", payload);
  };
  return (
    <form
      onSubmit={submit}
      aria-label={binding ? "Edit policy binding" : "Create policy binding"}
      className="space-y-5 rounded-lg border p-5"
    >
      <h2 className="text-lg font-semibold">{binding ? "Edit policy binding" : "Create policy binding"}</h2>
      <PolicyWriteError error={write.error} />
      {binding ? (
        <p className="text-sm">
          Target: <strong>{binding.target_label}</strong> ({targetKindLabels[binding.target_kind]})
        </p>
      ) : (
        <TargetPicker session={session} value={target} onChange={setTarget} disabled={blocked} />
      )}
      <PolicyChoice<RequestPolicy>
        path="/policies"
        session={session}
        value={policy}
        onChange={setPolicy}
        label="Request policy"
        disabled={blocked}
      />
      <label className="flex items-center gap-2 text-sm">
        <Checkbox checked={enabled} onCheckedChange={(value) => setEnabled(value === true)} disabled={blocked} />
        Apply policy
      </label>
      <p className="max-w-prose text-sm text-muted-foreground">
        Turn off “Apply policy” to stop applying this binding. Each target can have one binding. Restrictions combine in
        this order: public model, Team, user, then key. They do not grant model access or funds. Team policies follow
        each key’s native Team assignment; creating a binding does not assign team membership.
      </p>
      <div className="flex gap-2">
        <Button type="submit" disabled={saveDisabled}>
          {write.pending ? "Saving…" : "Save binding"}
        </Button>
        <Button type="button" variant="outline" onClick={onClose} disabled={write.pending}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
