import { describe, expect, it } from "vitest";
import { firstAllowedMonth, monthlyBudgetParsers, monthlyTimestamp, validStartMonth } from "./monthlyBudgetInput";

const boundary = Date.parse("2026-10-31T16:30:00Z");
describe("calendar month input boundaries", () => {
  it("uses the selected calendar timezone instead of the browser timezone", () => {
    expect(firstAllowedMonth("UTC", boundary)).toBe("2026-11");
    expect(firstAllowedMonth("Asia/Shanghai", boundary)).toBe("2026-12");
    expect(validStartMonth("2026-11", "UTC", boundary)).toBe(true);
    expect(validStartMonth("2026-11", "Asia/Shanghai", boundary)).toBe(false);
    expect(validStartMonth("2026-12", "Asia/Shanghai", boundary)).toBe(true);
  });
  it("rolls a December boundary into January of the following year", () => {
    expect(firstAllowedMonth("UTC", Date.parse("2026-12-15T00:00:00Z"))).toBe("2027-01");
  });
  it.each(["0000-12", "9999-01", "2026-00", "2026-13", "2026-1", "2026-10", "2026-11-01", " 2026-12"])(
    "rejects invalid or partial first month %s",
    (month) => {
      expect(validStartMonth(month, "Asia/Shanghai", boundary)).toBe(false);
    },
  );
  it("bounds URL pagination to the server offset limit", () => {
    expect(monthlyBudgetParsers.monthly_page.parse("401")).toBe(401);
    expect(monthlyBudgetParsers.monthly_page.parse("402")).toBeNull();
    expect(monthlyBudgetParsers.monthly_page.parse("-1")).toBeNull();
    expect(monthlyBudgetParsers.monthly_q.parse("a".repeat(101))).toBeNull();
  });
  it("renders a plan boundary in its own timezone and never echoes invalid timestamps", () => {
    const value = "2026-11-30T20:00:00Z";
    expect(monthlyTimestamp(value, "UTC")).not.toEqual(monthlyTimestamp(value, "Asia/Shanghai"));
    expect(monthlyTimestamp("invalid private diagnostic", "UTC")).toBe("Unavailable");
  });
});
