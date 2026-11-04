import { createParser } from "nuqs";
import { beijingTimeInput } from "../management/time";
import { managementTextParser } from "../management/ManagementShared";

export const modelBudgetPageSize = 25;
export const modelBudgetBrowseLimit = 10_025;
const budgetPageParser = createParser({
  parse: (value) => {
    const page = Number(value);
    return Number.isSafeInteger(page) && page >= 1 && page <= 401 ? page : null;
  },
  serialize: String,
}).withDefault(1);
export const modelBudgetListParsers = { budget_q: managementTextParser, budget_page: budgetPageParser };
export const modelBudgetModelParsers = { budget_model_q: managementTextParser, budget_model_page: budgetPageParser };

export function isModelBudgetAmount(value: string): boolean {
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,9})?$/.test(value)) return false;
  const [whole, fraction = ""] = value.split(".");
  if (whole.length > 10) return false;
  const nanos = BigInt(whole) * BigInt("1000000000") + BigInt(fraction.padEnd(9, "0"));
  return nanos > BigInt(0) && nanos <= BigInt("9000000000000000000");
}

type BudgetEnd = { ok: true; ends_at: string } | { ok: false; error: string };
function fixedWindowEnd(value: string, start: number, successor: boolean): BudgetEnd {
  const invalid = { ok: false as const, error: "Enter a valid end date and time in Beijing time." };
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return invalid;
  const date = new Date(`${value}:00+08:00`);
  if (Number.isNaN(date.getTime())) return invalid;
  if (beijingTimeInput(date.toISOString()) !== value) return invalid;
  const duration = date.getTime() - start;
  if (duration <= 0)
    return {
      ok: false,
      error: successor
        ? "Choose an end time after the next window starts."
        : "Choose an end time in the future. The server sets the start when the budget is created.",
    };
  if (duration > 366 * 24 * 60 * 60 * 1000)
    return {
      ok: false,
      error: successor
        ? "Choose an end time no more than 366 days after the next window starts."
        : "Choose an end time no more than 366 days ahead.",
    };
  return { ok: true, ends_at: date.toISOString() };
}

export function modelBudgetEnd(value: string, now: number): BudgetEnd {
  return fixedWindowEnd(value, now, false);
}

export function modelBudgetSuccessorEnd(value: string, startsAt: string): BudgetEnd {
  const start = Date.parse(startsAt);
  if (!Number.isFinite(start)) return { ok: false, error: "The next window start is unavailable. Reload budgets." };
  return fixedWindowEnd(value, start, true);
}
