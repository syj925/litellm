"use client";

import { useMutation } from "@tanstack/react-query";
import { useId, useRef, useState, type FormEvent } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { RetailRequestError, retailRequest } from "../api";
import { managementAttempt, managementConflict, type IdempotencyAttempt } from "../management/ManagementShared";
import BudgetModelPicker from "../model-budgets/BudgetModelPicker";
import { isModelBudgetAmount } from "../model-budgets/modelBudgetInput";
import { monthlyTimestamp, validStartMonth } from "./monthlyBudgetInput";
import MonthlyLimitFields from "./MonthlyLimitFields";
import {
  initialMonthlyValues,
  monthlyFormCopy,
  monthlySaveError,
  monthlyValidationFailure,
  stopExplanation,
} from "./monthlyFormCopy";
import type {
  AdminMonthlyBudget,
  CreateMonthlyBudget,
  MonthlyBudgetDraft,
  MonthlyTimezone,
  StopMonthlyBudget,
  UpdateMonthlyBudget,
} from "./types";

export default function MonthlyBudgetForm({
  userId,
  draft,
  onSaved,
  onCancel,
  onReload,
  onUnavailable,
  onSavingChange,
}: {
  userId: string;
  draft: MonthlyBudgetDraft;
  onSaved: () => void;
  onCancel: () => void;
  onReload: () => void;
  onUnavailable: () => void;
  onSavingChange: (saving: boolean) => void;
}) {
  const { accessToken, userId: actorId, userRole, isViewOnly, isAuthorized } = useAuthorized();
  const permitted = isAuthorized && Boolean(accessToken) && canUseAdminControlPlane({ userRole, isViewOnly });
  const id = useId();
  const plan = draft.kind === "create" ? null : draft.plan;
  const stopping = draft.kind === "stop";
  const initial = initialMonthlyValues(plan);
  const [model, setModel] = useState(initial.model);
  const [ready, setReady] = useState(Boolean(plan));
  const [amount, setAmount] = useState(initial.amount);
  const [timezone, setTimezone] = useState<MonthlyTimezone>(initial.timezone);
  const [month, setMonth] = useState("");
  const [openedAt] = useState(Date.now);
  const [validation, setValidation] = useState("");
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const options = {
    mutationFn: (payload: CreateMonthlyBudget | UpdateMonthlyBudget | StopMonthlyBudget) => {
      if (!permitted) throw new RetailRequestError("Administrator access is required.", 403);
      const base = `/admin/users/${encodeURIComponent(userId)}/monthly-budgets`;
      const path = plan ? `${base}/${encodeURIComponent(plan.id)}${stopping ? "/stop" : ""}` : base;
      return retailRequest<AdminMonthlyBudget>(path, accessToken, {
        method: plan && !stopping ? "PATCH" : "POST",
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
  const save = useMutation(options);
  const conflict = managementConflict(save.error);
  const busy = save.isPending || conflict;
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!permitted || busy || !ready) return;
    if (plan && stopping) {
      save.mutate({ expected_version: plan.version });
      return;
    }
    if (!isModelBudgetAmount(amount)) {
      setValidation("Enter a positive USD amount up to 9,000,000,000 with no more than 9 decimal places.");
      return;
    }
    if (plan) {
      setValidation("");
      save.mutate({ limit_usd: amount, expected_version: plan.version });
      return;
    }
    if (!validStartMonth(month, timezone, Date.now())) {
      setValidation("Choose the next calendar month or a later full month in the selected timezone.");
      return;
    }
    setValidation("");
    const payload: CreateMonthlyBudget = { model, start_month: month, timezone, limit_usd: amount, currency: "USD" };
    save.mutate(payload);
  };
  if (!permitted) return null;
  const copy = monthlyFormCopy(draft.kind);
  return (
    <section className="space-y-5" aria-label={copy.title}>
      <h3 className="text-base font-semibold">{copy.title}</h3>
      <form onSubmit={submit} className="space-y-5">
        {plan ? (
          <div className="space-y-2 text-sm">
            <p className="break-all font-medium">{plan.model}</p>
            <p className="text-muted-foreground">
              {plan.timezone} · Starts {monthlyTimestamp(plan.starts_at, plan.timezone)}
            </p>
          </div>
        ) : (
          <BudgetModelPicker
            accessToken={accessToken ?? ""}
            actorId={actorId}
            userId={userId}
            value={model}
            disabled={busy}
            onSelect={setModel}
            onReady={setReady}
            onUnavailable={onUnavailable}
          />
        )}
        {stopping ? (
          <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">
            {stopExplanation(plan?.state === "scheduled")}
          </p>
        ) : (
          <MonthlyLimitFields
            id={id}
            creating={!plan}
            amount={amount}
            timezone={timezone}
            month={month}
            openedAt={openedAt}
            busy={busy}
            onAmount={setAmount}
            onTimezone={setTimezone}
            onMonth={setMonth}
          />
        )}
        {!stopping && <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">{copy.note}</p>}
        {validation && (
          <p role="alert" className="text-sm text-destructive">
            {validation}
          </p>
        )}
        {save.error && (
          <Alert variant="error">
            <AlertTitle>{conflict ? "Monthly plan needs review" : "Unable to save monthly plan"}</AlertTitle>
            <AlertDescription>{monthlySaveError(save.error, conflict)}</AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="outline" disabled={save.isPending} onClick={onCancel}>
            Back to monthly plans
          </Button>
          {(conflict || monthlyValidationFailure(save.error)) && (
            <Button type="button" variant="outline" onClick={onReload}>
              Reload monthly plans
            </Button>
          )}
          <Button type="submit" disabled={busy || !ready}>
            {save.isPending ? "Saving..." : copy.submit}
          </Button>
        </div>
      </form>
    </section>
  );
}
