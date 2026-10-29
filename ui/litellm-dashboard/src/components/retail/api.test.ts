import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/networking", () => ({
  getGlobalLitellmHeaderName: () => "Authorization",
  getProxyBaseUrl: () => "",
}));

import {
  discountDescription,
  discountToPercent,
  formatMoney,
  isPositiveAmount,
  ledgerReceiptSections,
  percentToDiscount,
  usdPerTokenToPerMillion,
} from "./api";

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
  it("preserves the selected resource pool and its negotiated pricing source in a receipt", () => {
    expect(
      ledgerReceiptSections({
        price_snapshot: {
          plan_id: "shared",
          pool_id: "bedrock-pool",
          pool_name: "AWS Bedrock Claude",
          discount: "0.7",
          discount_model: "public-alpha",
          discount_source: "customer_pool_model",
        },
      }),
    ).toEqual([
      {
        title: "Pricing snapshot",
        rows: [
          { label: "Discount plan", value: "shared" },
          { label: "Resource pool", value: "AWS Bedrock Claude (bedrock-pool)" },
          { label: "Customer discount", value: "70% of reference price (30% off)" },
          { label: "Discount model", value: "public-alpha" },
          { label: "Discount source", value: "Customer pool-model override" },
        ],
      },
    ]);
  });
  it("identifies the exact negotiated model and rule source saved with the request", () => {
    expect(
      ledgerReceiptSections({
        price_snapshot: {
          plan_id: "shared",
          discount: "0.65",
          discount_model: "public-alpha",
          discount_source: "customer_model",
        },
      }),
    ).toEqual([
      {
        title: "Pricing snapshot",
        rows: [
          { label: "Discount plan", value: "shared" },
          { label: "Customer discount", value: "65% of reference price (35% off)" },
          { label: "Discount model", value: "public-alpha" },
          { label: "Discount source", value: "Customer model override" },
        ],
      },
    ]);
  });
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
          { label: "Discount plan", value: "contract-a" },
          { label: "Price version", value: "7" },
          { label: "Customer discount", value: "Legacy markup: 350% of reference price" },
          { label: "Input base price / 1M", value: "$5.00 USD" },
          { label: "Input customer price / 1M", value: "$17.50 USD" },
          { label: "Output base price / 1M", value: "$25.00 USD" },
          { label: "Output customer price / 1M", value: "$87.50 USD" },
          { label: "Cache read base price / 1M", value: "$0.50 USD" },
          { label: "Cache read customer price / 1M", value: "$1.75 USD" },
          { label: "Cache write base price / 1M", value: "$6.25 USD" },
          { label: "Cache write customer price / 1M", value: "$21.875 USD" },
        ],
      },
    ]);
  });

  it("shows the saved customer override and exact effective rates instead of applying a current plan", () => {
    const sections = ledgerReceiptSections({
      price_snapshot: {
        plan_id: "shared",
        discount: "0.701234567",
        multiplier: "1",
        discount_source: "customer",
        source: "catalog",
        source_label: "Reference catalog revision example",
        input_cost_per_token: "0.00000123456789123456789",
      },
    });
    expect(sections).toEqual([
      {
        title: "Pricing snapshot",
        rows: [
          { label: "Discount plan", value: "shared" },
          { label: "Customer discount", value: "70.1234567% of reference price (29.8765433% off)" },
          { label: "Discount source", value: "Customer override" },
          { label: "Reference source", value: "Reference catalog revision example" },
          { label: "Input reference price / 1M", value: "$1.23456789123456789 USD" },
          { label: "Input customer price / 1M", value: "$0.86572168064197530977625363 USD" },
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

describe("customer discounts", () => {
  it.each([
    ["70", "0.7"],
    ["100", "1"],
    ["70.1234567", "0.701234567"],
    ["0.0000001", "0.000000001"],
  ])("converts %s percent paid to exact discount %s", (percent, discount) => {
    expect(percentToDiscount(percent)).toBe(discount);
    expect(discountToPercent(discount)).toBe(percent);
  });

  it.each(["0", "100.0000001", "-1", "101", "1e2", "NaN", "0.00000001", "70.12345678", ""])(
    "rejects an invalid reference-price percentage %s",
    (percent) => expect(percentToDiscount(percent)).toBeNull(),
  );

  it("clearly distinguishes percentage paid from percentage saved and preserves legacy markups", () => {
    expect(discountDescription("0.7")).toBe("70% of reference price (30% off)");
    expect(discountDescription("1")).toBe("100% of reference price (0% off)");
    expect(discountDescription("3.5")).toBe("Legacy markup: 350% of reference price");
  });
});
