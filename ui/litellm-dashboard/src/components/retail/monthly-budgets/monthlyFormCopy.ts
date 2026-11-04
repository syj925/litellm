import type { AdminMonthlyBudget, MonthlyBudgetDraft, MonthlyTimezone } from "./types";
import { RetailRequestError } from "../api";

export function monthlyValidationFailure(error: Error | null): boolean {
  return error instanceof RetailRequestError && error.status === 422;
}

export function monthlySaveError(error: Error, conflict: boolean): string {
  if (conflict)
    return "The plan changed, stopped, or overlaps another budget. Reload and review before submitting again.";
  if (monthlyValidationFailure(error))
    return "Review the public model, first full month, timezone and exact positive USD amount. The server did not accept these terms.";
  return "The result could not be confirmed. Retry unchanged values to reuse this operation’s key, or reload the current plan.";
}

export function initialMonthlyValues(plan: AdminMonthlyBudget | null): {
  model: string;
  amount: string;
  timezone: MonthlyTimezone;
} {
  return { model: plan?.model ?? "", amount: plan?.limit_usd ?? "", timezone: plan?.timezone ?? "Asia/Shanghai" };
}

const copy = {
  create: {
    title: "Create monthly model budget",
    submit: "Create monthly plan",
    note: "The first window must be next month or later. Each month starts at midnight in this timezone with separate usage; unused capacity does not carry over.",
  },
  edit: {
    title: "Change future monthly limit",
    submit: "Save future limit",
    note: "The new limit applies from the next full month, or the plan’s later start. This month’s window is unchanged.",
  },
  stop: {
    title: "Stop monthly plan",
    submit: "Confirm stop",
    note: "",
  },
};
export function monthlyFormCopy(kind: MonthlyBudgetDraft["kind"]) {
  return copy[kind];
}

export function stopExplanation(scheduled: boolean): string {
  const timing = scheduled
    ? "If this plan has not started, cancellation removes all future windows. If a month has begun, its limit remains until month end."
    : "This month stays frozen and continues enforcing its limit until the month ends. Future windows are cancelled.";
  return `${timing} Requests already accepted still settle against their original month. This does not refund funds or disable the personal wallet.`;
}
