import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/networking", () => ({
  getGlobalLitellmHeaderName: () => "Authorization",
  getProxyBaseUrl: () => "",
}));

import { formatMoney, isPositiveAmount, ledgerReceiptSections, usdPerTokenToPerMillion } from "./api";

describe("wallet amounts", () => {
  it("preserves the smallest billable amount instead of displaying a zero charge", () => {
    expect(formatMoney("0.000000001")).toBe("$0.000000001");
    expect(formatMoney("-0.000000001")).toBe("-$0.000000001");
  });

  it("keeps large balances exact without converting through floating point", () => {
    expect(formatMoney("9007199254740993.123456789")).toBe("$9,007,199,254,740,993.123456789");
  });

  it("shows ordinary balances with cents and keeps nonzero fractional precision", () => {
    expect(formatMoney("25.000000000")).toBe("$25.00");
    expect(formatMoney("0.001540000")).toBe("$0.00154");
  });

  it.each(["0", "0.000000000", "-1", "1e3", "NaN", "1.0000000001", "10,000", ""])(
    "rejects an invalid or non-positive manual credit: %s",
    (value) => expect(isPositiveAmount(value)).toBe(false),
  );

  it.each(["0.000000001", "25.50", "1000"])("accepts a precise positive credit: %s", (value) => {
    expect(isPositiveAmount(value)).toBe(true);
  });
});

describe("customer billing receipts", () => {
  it.each([
    ["0.000005", "5"],
    ["5E-7", "0.5"],
    ["0.000000000000001", "0.000000001"],
    ["1.23456789123456789e-6", "1.23456789123456789"],
    ["9007199254740993.123456789", "9007199254740993123456.789"],
    ["0.000000000", "0"],
  ])("converts USD/token %s to an exact USD/1M rate", (input, expected) => {
    expect(usdPerTokenToPerMillion(input)).toBe(expected);
  });

  it("shows the nested API usage and saved sales price snapshot", () => {
    expect(
      ledgerReceiptSections({
        usage: { input_tokens: 8, output_tokens: 16, cache_read_tokens: 0, cache_write_tokens: 2 },
        price_snapshot: {
          plan_id: "contract-a",
          multiplier: "3.5",
          price_version: 7,
          input_cost_per_token: "0.000005",
          output_cost_per_token: "0.000025",
          cache_read_input_token_cost: "5E-7",
          cache_creation_input_token_cost: "0.00000625",
        },
      }),
    ).toEqual([
      {
        title: "Token usage",
        rows: [
          { label: "Input tokens (uncached)", value: "8" },
          { label: "Output tokens", value: "16" },
          { label: "Cache read tokens", value: "0" },
          { label: "Cache write tokens", value: "2" },
        ],
      },
      {
        title: "Pricing snapshot",
        rows: [
          { label: "Pricing plan", value: "contract-a" },
          { label: "Plan multiplier", value: "3.5×" },
          { label: "Price version", value: "7" },
          { label: "Input base price / 1M", value: "$5.00 USD" },
          { label: "Output base price / 1M", value: "$25.00 USD" },
          { label: "Cache read base price / 1M", value: "$0.50 USD" },
          { label: "Cache write base price / 1M", value: "$6.25 USD" },
        ],
      },
    ]);
  });

  it("never renders provider costs or arbitrary fields, even if present in a response", () => {
    expect(
      ledgerReceiptSections({
        provider_cost: "private",
        usage: { provider_cost: "private", input_tokens: -1 },
        price_snapshot: { provider_cost: "private", input_cost_per_token: "NaN" },
      }),
    ).toEqual([]);
  });
});
