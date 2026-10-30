import { describe, expect, it } from "vitest";
import { fixedRatesExpression, parseUsageBounds, parsePreviewParameters, policyFromRates } from "./expressionPricing";

const rates = {
  model: "fixture",
  input_per_million: "2",
  output_per_million: "8",
  cache_read_per_million: "0.2",
  cache_write_per_million: "2.5",
};

describe("expression drafts", () => {
  it("preserves complete cache write prices without charging classified duration tokens twice", () => {
    const expression = fixedRatesExpression({ ...rates, cache_write_tiers: { "300": "3", "3600": "4" } });
    expect(expression).toBe("p * 2 + c * 8 + cr * 0.2 + cc * 2.5 + cc5m * 3 + cc1h * 4");
    expect(policyFromRates(rates)).toEqual({
      version: "expression-v1",
      expression: "p * 2 + c * 8 + cr * 0.2 + cc * 2.5",
      timezone: "Asia/Shanghai",
      usage_bounds: {},
    });
  });

  it("retains exact decimal reservation bounds and rejects invalid objects instead of dropping them", () => {
    const bounds = { images: "4", seconds: "9007199254740993.123456789" };
    expect(parseUsageBounds(JSON.stringify(bounds))).toEqual({ bounds, error: null });
    for (const value of ['{"images":4}', '{"images":"-1"}', '{"images":"NaN"}', "[]", "null", "{"]) {
      expect(parseUsageBounds(value).error).not.toBeNull();
    }
  });
  it("copies custom duration prices and explicit zero rates without dropping or double charging the subsets", () => {
    const policy = policyFromRates({ ...rates, cache_write_tiers: { "75": "9", "300": "3", "750": "0" } });
    expect(policy.expression).toBe(
      "p * 2 + c * 8 + cr * 0.2 + cc * 2.5 + cache_write('75') * 9 + cc5m * 3 + cache_write('750') * 0",
    );
    expect(policy.usage_bounds).toEqual({});
  });
  it("permits only synthetic request parameters used by the pricing compiler", () => {
    expect(parsePreviewParameters('{"quality":"high","duration":60}')).toEqual({
      params: { quality: "high", duration: 60 },
      error: null,
    });
    expect(parsePreviewParameters('{"prompt":"private data"}').error).not.toBeNull();
    expect(parsePreviewParameters('{"quality":{}}').error).not.toBeNull();
  });
});
