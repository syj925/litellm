import { describe, expect, it } from "vitest";
import { businessLabelsParsers, parseBusinessLabels } from "./businessLabels";

describe("business label input", () => {
  it("keeps exact values and permits clearing all labels", () => {
    expect(parseBusinessLabels("support\nproduction-2\n")).toEqual({ ok: true, tags: ["support", "production-2"] });
    expect(parseBusinessLabels("")).toEqual({ ok: true, tags: [] });
    expect(parseBusinessLabels("z".repeat(40)).ok).toBe(true);
  });
  it.each([
    "Uppercase",
    " leading",
    "trailing ",
    "two--hyphens",
    "-prefix",
    "suffix-",
    "123",
    "not_valid",
    "含有中文",
    "z".repeat(41),
    "support\nsupport",
    Array.from({ length: 11 }, (_, i) => `group-${i}`).join("\n"),
    "abcdef0123456789",
    "a1234567890123456",
    "a1234567-1234-1234-1234-123456789abc",
  ])("rejects invalid or identifier-like labels: %s", (value) => {
    expect(parseBusinessLabels(value).ok).toBe(false);
  });
});

describe("business label route state", () => {
  it("allows opaque object references and rejects raw credentials or token hashes", () => {
    const id = "23648472-a962-4cd4-ad96-ce9c093b44d1";
    expect(businessLabelsParsers.target.parse(id)).toBe(id);
    expect(businessLabelsParsers.target.parse("f".repeat(64))).toBeNull();
    expect(businessLabelsParsers.target.parse("sk-not-an-object-reference")).toBeNull();
    expect(businessLabelsParsers.page.parse("402")).toBeNull();
    expect(businessLabelsParsers.kind.parse("provider")).toBeNull();
  });
});
