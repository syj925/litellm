import { createParser, parseAsStringLiteral } from "nuqs";
import { managementTextParser } from "../management/ManagementShared";
import type { MonthlyTimezone } from "./types";

const monthlyPageParser = createParser({
  parse: (value) => {
    const page = Number(value);
    return Number.isSafeInteger(page) && page >= 1 && page <= 401 ? page : null;
  },
  serialize: String,
}).withDefault(1);
export const monthlyBudgetParsers = { monthly_q: managementTextParser, monthly_page: monthlyPageParser };
export const budgetModeParser = parseAsStringLiteral(["fixed", "monthly"]).withDefault("fixed");

export function firstAllowedMonth(timezone: MonthlyTimezone, now: number): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
  }).formatToParts(new Date(now));
  const year = Number(parts.find((part) => part.type === "year")?.value);
  const month = Number(parts.find((part) => part.type === "month")?.value);
  return `${year + Math.floor(month / 12)}-${String((month % 12) + 1).padStart(2, "0")}`;
}

export function validStartMonth(value: string, timezone: MonthlyTimezone, now: number): boolean {
  if (!/^\d{4}-(?:0[1-9]|1[0-2])$/.test(value)) return false;
  const year = Number(value.slice(0, 4));
  if (year < 1 || year > 9998) return false;
  return value >= firstAllowedMonth(timezone, now);
}

export function monthlyTimestamp(value: string, timezone: MonthlyTimezone): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unavailable"
    : date.toLocaleString("en-GB", {
        timeZone: timezone,
        dateStyle: "medium",
        timeStyle: "short",
      });
}
