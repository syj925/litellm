import { describe, expect, it } from "vitest";
import { RetailRequestError } from "../api";
import {
  assignmentLabel,
  billingPayload,
  consumptionParsers,
  consumptionWindow,
  financeAccessLost,
  financeError,
} from "./financeInput";

describe("organization finance input", () => {
  it("requires an explicit assignment and preserves exact organization IDs", () => {
    expect(billingPayload("", 0)).toBeNull();
    expect(billingPayload("organization:", 0)).toBeNull();
    expect(billingPayload("organization:org-two", 3)).toEqual({ organization_id: "org-two", expected_version: 3 });
    expect(billingPayload("clear", 3)).toEqual({ organization_id: null, expected_version: 3 });
    expect(billingPayload("clear", -1)).toBeNull();
    expect(billingPayload("clear", 1.5)).toBeNull();
  });
  it("keeps a disabled organization distinct from an empty assignment", () => {
    expect(assignmentLabel("organization_disabled")).toContain("organization disabled");
    expect(assignmentLabel("membership_inactive")).toContain("membership unavailable");
    expect(assignmentLabel("unassigned")).toBe("No billing organization");
  });
  it("rejects one-sided, reversed, and overly broad windows", () => {
    const start = new Date("2026-10-01T00:00:00Z");
    const end = new Date("2026-11-01T00:00:00Z");
    expect(consumptionWindow(null, null)).toBeNull();
    expect(consumptionWindow(start, null)).toContain("both");
    expect(consumptionWindow(end, start)).toContain("after");
    expect(consumptionWindow(start, end)).toBeNull();
    expect(consumptionWindow(start, new Date("2026-11-01T00:00:01Z"))).toContain("31 days");
  });
  it("rejects malformed URL identifiers and invalid dates in parsers", () => {
    expect(consumptionParsers.organization.parse("org one")).toBeNull();
    expect(consumptionParsers.organization.parse("o".repeat(192))).toBeNull();
    expect(consumptionParsers.start.parse("invalid")).toBeNull();
    expect(consumptionParsers.organization.parse("org-one")).toBe("org-one");
  });
  it("hides internal server errors and distinguishes a missing target from lost authority", () => {
    const missing = new RetailRequestError("private-source-marker", 404);
    expect(financeAccessLost(missing)).toBe(false);
    expect(financeAccessLost(missing, true)).toBe(true);
    expect(financeAccessLost(new RetailRequestError("private-source-marker", 403))).toBe(true);
    expect(financeError(missing, "assignment")).not.toContain("private-source-marker");
    expect(financeError(new RetailRequestError("private-source-marker", 500), "assignment")).not.toContain(
      "private-source-marker",
    );
  });
  it("keeps validation recovery specific to the finance operation", () => {
    const invalid = new RetailRequestError("private-source-marker", 422);
    expect(financeError(invalid, "assignment")).toContain("choose an active membership");
    expect(financeError(invalid, "assignment")).not.toContain("window");
    expect(financeError(invalid, "organizations")).toContain("Adjust the search");
    expect(financeError(invalid, "consumption")).toContain("at most 10,000 requests");
    expect(financeError(invalid, "assignment")).not.toContain("private-source-marker");
  });
});
