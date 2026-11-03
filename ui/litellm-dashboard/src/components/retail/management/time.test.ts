import { describe, expect, it } from "vitest";
import { beijingTimeInput, managementInterval } from "./time";

describe("management time windows", () => {
  it("converts Beijing form values to an exclusive UTC window", () => {
    expect(managementInterval("2026-10-01T00:00", "2026-11-01T00:00", true)).toEqual({
      ok: true,
      interval: { start_at: "2026-09-30T16:00:00.000Z", end_at: "2026-10-31T16:00:00.000Z" },
    });
    expect(beijingTimeInput("2026-09-30T16:00:00.000Z")).toBe("2026-10-01T00:00");
  });

  it.each([
    ["2026-10-01T00:00", "2026-11-01T00:01"],
    ["2026-10-01T00:00", "2026-10-01T00:00"],
    ["2026-10-02T00:00", "2026-10-01T00:00"],
    ["2026-13-01T00:00", "2026-13-02T00:00"],
    ["2026-02-30T00:00", "2026-03-01T00:00"],
    ["2026-10-01T00:00", ""],
  ])("rejects invalid, unpaired or oversized windows: %s to %s", (from, until) => {
    expect(managementInterval(from, until, true).ok).toBe(false);
  });

  it("allows omitted audit windows while requiring report windows", () => {
    expect(managementInterval("", "")).toEqual({ ok: true, interval: null });
    expect(managementInterval("", "", true).ok).toBe(false);
  });
});
