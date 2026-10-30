import { describe, expect, it } from "vitest";
import { cacheDurationLabel, cacheTierEntries, validateCacheTierDrafts } from "./cachePricing";

describe("cache pricing durations", () => {
  it("labels known and custom durations without guessing missing values", () => {
    expect([300, 1800, 3600, 43200, 75, 0].map(cacheDurationLabel)).toEqual([
      "5m",
      "30m",
      "1h",
      "12h",
      "75s",
      "Unspecified duration",
    ]);
    expect(cacheTierEntries({ "43200": "8", "300": "0", "3600": "4" })).toEqual([
      ["300", "0"],
      ["3600", "4"],
      ["43200", "8"],
    ]);
  });

  it("preserves zero prices and rejects duplicate canonical durations and incomplete rates", () => {
    expect(
      validateCacheTierDrafts([
        { ttl_seconds: "300", price: "0" },
        { ttl_seconds: "75", price: "2.123456789" },
      ]),
    ).toEqual({ tiers: { "300": "0", "75": "2.123456789" }, error: null });
    expect(
      validateCacheTierDrafts([
        { ttl_seconds: "0300", price: "1" },
        { ttl_seconds: "300", price: "2" },
      ]).error,
    ).toMatch(/only one price/);
    expect(validateCacheTierDrafts([{ ttl_seconds: "1.5", price: "1" }]).error).toMatch(/whole seconds/);
    expect(validateCacheTierDrafts([{ ttl_seconds: "300", price: "" }]).error).toMatch(/every cache duration/);
    expect(validateCacheTierDrafts([{ ttl_seconds: "300", price: "-1" }]).error).toMatch(/non-negative/);
  });
});
