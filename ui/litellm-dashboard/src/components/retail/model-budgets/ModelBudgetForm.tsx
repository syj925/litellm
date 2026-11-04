"use client";

import { useMutation } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { useId, useRef, useState, type FormEvent } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RetailRequestError, retailRequest } from "../api";
import {
  managementAttempt,
  managementConflict,
  managementTimestamp,
  type IdempotencyAttempt,
} from "../management/ManagementShared";
import BudgetModelPicker from "./BudgetModelPicker";
import { isModelBudgetAmount, modelBudgetEnd, modelBudgetSuccessorEnd } from "./modelBudgetInput";
import type {
  AdminModelBudget,
  CreateModelBudget,
  ModelBudgetDraft,
  ScheduleModelBudget,
  UpdateModelBudget,
} from "./types";

function initialValues(budget: AdminModelBudget | null) {
  if (!budget) return { model: "", amount: "", enabled: true, ready: false };
  return { model: budget.model, amount: budget.limit_usd, enabled: budget.enabled, ready: true };
}

function saveLabel(pending: boolean, kind: ModelBudgetDraft["kind"]) {
  if (pending) return "Saving...";
  if (kind === "successor") return "Schedule next window";
  return kind === "edit" ? "Save budget" : "Create budget";
}

function formCopy(draft: ModelBudgetDraft) {
  if (draft.kind === "create") return { label: "Create fixed-window budget", title: "Create a model budget" };
  if (draft.kind === "successor") {
    return { label: "Schedule next fixed-window budget", title: `Schedule next window for ${draft.budget.model}` };
  }
  return { label: "Edit fixed-window budget", title: `Edit budget for ${draft.budget.model}` };
}

export default function ModelBudgetForm({
  accessToken,
  actorId,
  userId,
  draft,
  onSaved,
  onCancel,
  onReload,
  onUnavailable,
  onSavingChange,
}: {
  accessToken: string;
  actorId: string | null;
  userId: string;
  draft: ModelBudgetDraft;
  onSaved: () => void;
  onCancel: () => void;
  onReload: () => void;
  onUnavailable: () => void;
  onSavingChange: (saving: boolean) => void;
}) {
  const id = useId();
  const budget = draft.kind === "create" ? null : draft.budget;
  const scheduling = draft.kind === "successor";
  const editing = draft.kind === "edit";
  const copy = formCopy(draft);
  const initial = initialValues(budget);
  const [model, setModel] = useState(initial.model);
  const [modelReady, setModelReady] = useState(initial.ready);
  const [amount, setAmount] = useState(initial.amount);
  const [endsAt, setEndsAt] = useState("");
  const [enabled, setEnabled] = useState(initial.enabled);
  const [validation, setValidation] = useState("");
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const mutationOptions = {
    mutationFn: (payload: CreateModelBudget | UpdateModelBudget | ScheduleModelBudget) => {
      const path = `/admin/users/${encodeURIComponent(userId)}/model-budgets`;
      const target = budget ? `${path}/${encodeURIComponent(budget.id)}${scheduling ? "/successor" : ""}` : path;
      return retailRequest<AdminModelBudget>(target, accessToken, {
        method: editing ? "PATCH" : "POST",
        headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
        body: JSON.stringify(payload),
      });
    },
    onMutate: () => onSavingChange(true),
    onSuccess: onSaved,
    onSettled: () => onSavingChange(false),
    onError: (error: Error) => {
      if (error instanceof RetailRequestError && [401, 403, 404].includes(error.status)) onUnavailable();
    },
  };
  const save = useMutation(mutationOptions);
  const conflict = managementConflict(save.error);
  const busy = save.isPending || conflict;
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !modelReady) return;
    if (!isModelBudgetAmount(amount)) {
      setValidation("Enter a positive USD amount up to 9,000,000,000 with no more than 9 decimal places.");
      return;
    }
    if (editing && budget) {
      setValidation("");
      save.mutate({ limit_usd: amount, enabled, expected_version: budget.config_version });
      return;
    }
    const end =
      scheduling && budget ? modelBudgetSuccessorEnd(endsAt, budget.ends_at) : modelBudgetEnd(endsAt, Date.now());
    if (!end.ok) {
      setValidation(end.error);
      return;
    }
    setValidation("");
    if (scheduling && budget) {
      const payload = { limit_usd: amount, ends_at: end.ends_at, enabled, expected_version: budget.config_version };
      save.mutate(payload);
      return;
    }
    const payload = { model, limit_usd: amount, ends_at: end.ends_at, enabled };
    save.mutate(payload);
  };
  return (
    <section className="space-y-5" aria-label={copy.label}>
      <h3 className="text-base font-semibold">{copy.title}</h3>
      <form onSubmit={submit} className="space-y-5">
        {budget ? (
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
            <dt className="text-muted-foreground">Public model</dt>
            <dd className="break-all">{budget.model}</dd>
            <dt className="text-muted-foreground">
              {scheduling ? "Next window starts (Beijing)" : "Effective from (Beijing)"}
            </dt>
            <dd>{managementTimestamp(scheduling ? budget.ends_at : budget.effective_from)}</dd>
            {editing && (
              <>
                <dt className="text-muted-foreground">Until (exclusive, Beijing)</dt>
                <dd>{managementTimestamp(budget.ends_at)}</dd>
                <dt className="text-muted-foreground">Configuration version</dt>
                <dd>{budget.config_version}</dd>
              </>
            )}
          </dl>
        ) : (
          <BudgetModelPicker
            accessToken={accessToken}
            actorId={actorId}
            userId={userId}
            value={model}
            disabled={busy}
            onSelect={setModel}
            onReady={setModelReady}
            onUnavailable={onUnavailable}
          />
        )}
        <div className="grid gap-5 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor={`${id}-amount`}>Budget limit (USD)</Label>
            <Input
              id={`${id}-amount`}
              inputMode="decimal"
              value={amount}
              maxLength={20}
              disabled={busy}
              onChange={(event) => setAmount(event.target.value)}
              required
              autoComplete="off"
            />
          </div>
          {!editing && (
            <div className="space-y-2">
              <Label htmlFor={`${id}-end`}>End time (Beijing)</Label>
              <Input
                id={`${id}-end`}
                type="datetime-local"
                value={endsAt}
                disabled={busy}
                onChange={(event) => setEndsAt(event.target.value)}
                required
              />
              <p className="text-xs leading-relaxed text-muted-foreground">
                {scheduling
                  ? "Choose an end after the start shown above, within 366 days. The server fixes the start to the current window’s end."
                  : "Choose a future end time within 366 days. The server sets the start when this budget is created."}
              </p>
            </div>
          )}
        </div>
        <div className="space-y-2">
          <label className="flex items-center gap-3">
            <Checkbox checked={enabled} onCheckedChange={(value) => setEnabled(value === true)} disabled={busy} />
            <span className="text-sm font-medium">Enforce this budget</span>
          </label>
          <p className="text-xs leading-relaxed text-muted-foreground">
            Pausing enforcement keeps usage tracking and does not reset totals. Overlapping windows for the same public
            model are not allowed, including paused windows.
          </p>
        </div>
        {editing && (
          <p className="text-xs leading-relaxed text-muted-foreground">
            Lowering the limit below settled and held amounts stops new requests. Requests already in progress still
            settle against this window. The model and end time cannot be changed.
          </p>
        )}
        {scheduling && (
          <p className="text-xs leading-relaxed text-muted-foreground">
            This creates one separate window. Its usage starts at zero; unused capacity does not carry over. Requests
            already in progress still settle against the current window. This does not enable automatic renewal.
          </p>
        )}
        {validation && (
          <p role="alert" className="text-sm text-destructive">
            {validation}
          </p>
        )}
        {save.error && (
          <Alert variant="error">
            <AlertTitle>{conflict ? "Budget needs review" : "Unable to save model budget"}</AlertTitle>
            <AlertDescription>
              {conflict
                ? "The configuration changed, the current window ended, or another window already exists. Reload budgets and review the current records before submitting again."
                : save.error.message}
            </AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="outline" disabled={save.isPending} onClick={onCancel}>
            Back to budgets
          </Button>
          {conflict && (
            <Button type="button" variant="outline" onClick={onReload}>
              Reload budgets
            </Button>
          )}
          <Button type="submit" disabled={busy || !modelReady}>
            <Save />
            {saveLabel(save.isPending, draft.kind)}
          </Button>
        </div>
      </form>
      {editing && budget && (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 border-t pt-5 text-xs">
          <dt className="text-muted-foreground">Budget rule created</dt>
          <dd>{managementTimestamp(budget.created_at)}</dd>
          <dt className="text-muted-foreground">Budget rule created by</dt>
          <dd className="break-all">{budget.created_by}</dd>
          <dt className="text-muted-foreground">Budget rule updated</dt>
          <dd>{managementTimestamp(budget.updated_at)}</dd>
          <dt className="text-muted-foreground">Budget rule updated by</dt>
          <dd className="break-all">{budget.updated_by}</dd>
          <dt className="text-muted-foreground">Source</dt>
          <dd>Management API</dd>
        </dl>
      )}
    </section>
  );
}
