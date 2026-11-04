import { describe, expect, it } from "vitest";
import { beijingTimeInput } from "../management/time";
import {
  isModelBudgetAmount,
  modelBudgetBrowseLimit,
  modelBudgetEnd,
  modelBudgetListParsers,
  modelBudgetSuccessorEnd,
} from "./modelBudgetInput";

describe("model budget money", () => {
  it.each(["0.000000001", "1.123456789", "1234567890.123456789", "9000000000.000000000"])(
    "accepts exact positive USD amounts: %s",
    (value) => expect(isModelBudgetAmount(value)).toBe(true),
  );
  it.each([
    "0",
    "0.000000000",
    "-1",
    "1e3",
    "1.0000000001",
    "9000000000.000000001",
    "9000000001",
    "01",
    " 1",
    "1 ",
    "9".repeat(1000),
  ])("rejects invalid or out-of-range USD amounts: %s", (value) => expect(isModelBudgetAmount(value)).toBe(false));
});

describe("successor budget end time", () => {
  const start = "2026-12-04T00:00:00.123Z";
  it("uses the exact predecessor end instead of the current time", () => {
    expect(modelBudgetSuccessorEnd("2026-12-04T08:01", start)).toEqual({
      ok: true,
      ends_at: "2026-12-04T00:01:00.000Z",
    });
    expect(modelBudgetSuccessorEnd("2026-12-04T08:00", start)).toEqual({
      ok: false,
      error: "Choose an end time after the next window starts.",
    });
  });
  it("measures the 366-day limit from the scheduled start", () => {
    const boundary = Date.parse(start) + 366 * 24 * 60 * 60 * 1000;
    expect(modelBudgetSuccessorEnd(beijingTimeInput(new Date(boundary).toISOString()), start).ok).toBe(true);
    expect(modelBudgetSuccessorEnd(beijingTimeInput(new Date(boundary + 60_000).toISOString()), start)).toEqual({
      ok: false,
      error: "Choose an end time no more than 366 days after the next window starts.",
    });
  });
  it.each(["", "2026-02-30T08:00", "2026-13-01T08:00"])("rejects invalid Beijing dates: %s", (value) => {
    expect(modelBudgetSuccessorEnd(value, start).ok).toBe(false);
  });
  it("requires a valid server start before accepting the next end", () => {
    expect(modelBudgetSuccessorEnd("2026-12-05T08:00", "unknown")).toEqual({
      ok: false,
      error: "The next window start is unavailable. Reload budgets.",
    });
  });
});

describe("model budget end time", () => {
  const now = Date.UTC(2026, 10, 4);
  it("converts Beijing input into the exact timezone-aware end value", () => {
    expect(modelBudgetEnd("2026-11-05T08:00", now)).toEqual({ ok: true, ends_at: "2026-11-05T00:00:00.000Z" });
  });
  it("permits the 366-day boundary and rejects a minute beyond it", () => {
    const boundary = now + 366 * 24 * 60 * 60 * 1000;
    expect(modelBudgetEnd(beijingTimeInput(new Date(boundary).toISOString()), now).ok).toBe(true);
    expect(modelBudgetEnd(beijingTimeInput(new Date(boundary + 60_000).toISOString()), now).ok).toBe(false);
  });
  it.each(["2026-11-04T08:00", "2026-11-03T08:00", "2026-02-30T08:00", "2026-13-01T08:00", ""])(
    "rejects invalid or elapsed end times: %s",
    (value) => expect(modelBudgetEnd(value, now).ok).toBe(false),
  );
  it("keeps browsing inside the server offset boundary", () => {
    expect(modelBudgetListParsers.budget_page.parse("401")).toBe(401);
    expect(modelBudgetListParsers.budget_page.parse("402")).toBeNull();
    expect(modelBudgetBrowseLimit).toBe(10_025);
  });
});
