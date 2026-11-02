import { describe, expect, it } from "vitest";
import {
  baselineTokenPrice,
  feeUnitLabel,
  tokenPriceColumns,
  tokenPriceLabel,
  type ExpressionPriceDisplay,
} from "./priceDisplay";

describe("price display", () => {
  it("uses one explicitly identified scenario rather than combining minimum rates from different conditions", () => {
    const display: ExpressionPriceDisplay = {
      status: "complete",
      token_rates: [
        { conditions: ["Special service"], rates: { p: "0.5", c: "100" }, baseline: false },
        { conditions: ["Standard", "Short context"], rates: { p: "2", c: "8", cr: "0.2" }, baseline: true },
      ],
      fees: [],
      modifiers: [],
      notes: [],
    };
    expect(baselineTokenPrice(display)).toEqual(display.token_rates[1]);
    expect(baselineTokenPrice({ ...display, status: "unsupported" })).toBeNull();
    expect(
      baselineTokenPrice({ ...display, token_rates: [{ ...display.token_rates[0], baseline: false }] }),
    ).toBeNull();
    expect(tokenPriceColumns(display.token_rates)).toEqual(["p", "c", "cr"]);
  });

  it("labels custom cache durations and different billing units without exposing variable identifiers", () => {
    expect(tokenPriceLabel("cache_write_750")).toBe("Cache write · 750s");
    expect(tokenPriceLabel("ai_cr")).toBe("Cached audio input");
    expect(feeUnitLabel("grounded_request")).toBe("per grounded request");
    expect(feeUnitLabel("query")).toBe("per query");
  });
});
