import { describe, expect, it } from "vitest";
import { cacheDurationLabel, cacheTierEntries, cacheWritePriceRows, validateCacheTierDrafts } from "./cachePricing";
import { catalogPriceRows } from "./catalog/types";

describe("cache pricing durations", () => {
  it("shows configured duration prices instead of repeating the default even when the prices are equal", () => {
    expect(cacheWritePriceRows("2.5", { "300": "2.5", "3600": "2.5", "75": "0" })).toEqual([
      { key: "cache_write_75", label: "Cache write · 75s", amount: "0" },
      { key: "cache_write_300", label: "Cache write · 5m", amount: "2.5" },
      { key: "cache_write_3600", label: "Cache write · 1h", amount: "2.5" },
    ]);
    expect(cacheWritePriceRows("0", {})).toEqual([{ key: "cache_write", label: "Cache write", amount: "0" }]);
    expect(cacheWritePriceRows("4", undefined)).toEqual([{ key: "cache_write", label: "Cache write", amount: "4" }]);
    const flatPrice = { input: "1", output: "2", cache_read: "0", cache_write: "4" };
    expect(catalogPriceRows(flatPrice)).toEqual([
      { key: "input", label: "Input", amount: "1" },
      { key: "output", label: "Output", amount: "2" },
      { key: "cache_read", label: "Cache read", amount: "0" },
      { key: "cache_write", label: "Cache write", amount: "4" },
    ]);
  });

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
