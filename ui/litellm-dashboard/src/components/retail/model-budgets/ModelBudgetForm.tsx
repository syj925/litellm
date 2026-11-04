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
import { isModelBudgetAmount, modelBudgetEnd } from "./modelBudgetInput";
import type { AdminModelBudget, CreateModelBudget, UpdateModelBudget } from "./types";

function initialValues(budget: AdminModelBudget | null) {
  if (!budget) return { model: "", amount: "", enabled: true, ready: false };
  return { model: budget.model, amount: budget.limit_usd, enabled: budget.enabled, ready: true };
}

function saveLabel(pending: boolean, editing: boolean) {
  if (pending) return "Saving...";
  return editing ? "Save budget" : "Create budget";
}

export default function ModelBudgetForm({
  accessToken,
  actorId,
  userId,
  budget,
  onSaved,
  onCancel,
  onReload,
  onUnavailable,
  onSavingChange,
}: {
  accessToken: string;
  actorId: string | null;
  userId: string;
  budget: AdminModelBudget | null;
  onSaved: () => void;
  onCancel: () => void;
  onReload: () => void;
  onUnavailable: () => void;
  onSavingChange: (saving: boolean) => void;
}) {
  const id = useId();
  const initial = initialValues(budget);
  const [model, setModel] = useState(initial.model);
  const [modelReady, setModelReady] = useState(initial.ready);
  const [amount, setAmount] = useState(initial.amount);
  const [endsAt, setEndsAt] = useState("");
  const [enabled, setEnabled] = useState(initial.enabled);
  const [validation, setValidation] = useState("");
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const mutationOptions = {
    mutationFn: (payload: CreateModelBudget | UpdateModelBudget) => {
      const path = `/admin/users/${encodeURIComponent(userId)}/model-budgets`;
      return retailRequest<AdminModelBudget>(budget ? `${path}/${encodeURIComponent(budget.id)}` : path, accessToken, {
        method: budget ? "PATCH" : "POST",
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
    if (budget) {
      setValidation("");
      save.mutate({ limit_usd: amount, enabled, expected_version: budget.config_version });
      return;
    }
    const end = modelBudgetEnd(endsAt, Date.now());
    if (!end.ok) {
      setValidation(end.error);
      return;
    }
    setValidation("");
    const payload = { model, limit_usd: amount, ends_at: end.ends_at, enabled };
    save.mutate(payload);
  };
  return (
    <section className="space-y-5" aria-label={budget ? "Edit fixed-window budget" : "Create fixed-window budget"}>
      <h3 className="text-base font-semibold">
        {budget ? `Edit budget for ${budget.model}` : "Create a model budget"}
      </h3>
      <form onSubmit={submit} className="space-y-5">
        {budget ? (
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
            <dt className="text-muted-foreground">Public model</dt>
            <dd className="break-all">{budget.model}</dd>
            <dt className="text-muted-foreground">Effective from (Beijing)</dt>
            <dd>{managementTimestamp(budget.effective_from)}</dd>
            <dt className="text-muted-foreground">Until (exclusive, Beijing)</dt>
            <dd>{managementTimestamp(budget.ends_at)}</dd>
            <dt className="text-muted-foreground">Configuration version</dt>
            <dd>{budget.config_version}</dd>
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
          {!budget && (
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
                Choose a future end time within 366 days. The server sets the start when this budget is created.
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
        {budget && (
          <p className="text-xs leading-relaxed text-muted-foreground">
            Lowering the limit below settled and held amounts stops new requests. Requests already in progress still
            settle against this window. The model and end time cannot be changed.
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
                ? "The configuration changed or the window overlaps another budget. Reload budgets and review the current records before editing again."
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
            {saveLabel(save.isPending, Boolean(budget))}
          </Button>
        </div>
      </form>
      {budget && (
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
