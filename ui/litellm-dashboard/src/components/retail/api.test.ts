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
  ledgerEntriesCsv,
  ledgerReceiptSections,
  percentToDiscount,
  retailRequest,
  usdPerTokenToPerMillion,
} from "./api";

describe("wallet amounts", () => {
  it("sends a management write with its authentication, idempotency key and abort signal without caching", async () => {
    const controller = new AbortController();
    const body = JSON.stringify({ name: "Fixture organization", admin_user_id: "member" });
    const request = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response('{"id":"org-fixture"}'));
    try {
      const result = await retailRequest("/admin/organizations", "fixture", {
        method: "POST", body, signal: controller.signal, headers: { "Idempotency-Key": "fixture-operation" },
      });
      expect(result).toEqual({ id: "org-fixture" });
      expect(request).toHaveBeenCalledWith("/retail/admin/organizations", expect.objectContaining({
        method: "POST", body, signal: controller.signal, credentials: "same-origin", cache: "no-store",
        headers: expect.objectContaining({ Authorization: "Bearer fixture", "idempotency-key": "fixture-operation" }),
      }));
    } finally {
      request.mockRestore();
    }
  });

  it("shows the billing compiler validation message without echoing rejected request data", async () => {
    const request = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: [{ msg: "Value error, unknown pricing variable", input: { secret: "never display" } }],
        }),
        { status: 422 },
      ),
    );
    try {
      await expect(retailRequest("/admin/pricing-preview", "fixture")).rejects.toMatchObject({
        message: "Value error, unknown pricing variable",
      });
    } finally {
      request.mockRestore();
    }
  });
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
  it("exports the actual expression charge, branch, timestamp and evaluated usage without fallback flat prices", () => {
    const details = {
      price_snapshot: {
        input_cost_per_token: "0.000099",
        discount: "0.5",
        pool_name: "Fixture pool",
        pool_id: "fixture",
      },
      expression_pricing: {
        version: "expression-v1" as const,
        expression_hash: "a".repeat(64),
        expression: "tier('peak', p * 3 + fixed(u('images') * 0.04))",
        timezone: "Asia/Shanghai",
        started_at: "2026-01-05T10:00:00+08:00",
        tiers: ["peak"],
        variables: { p: "1000", len: "200001" },
        usage: { images: "2" },
        undiscounted_amount: "0.083",
        discount: "0.5",
        amount: "0.0415",
      },
    };
    const sections = ledgerReceiptSections(details);
    const rule = sections.find(({ title }) => title === "Dynamic pricing receipt");
    expect(rule?.rows).toEqual(
      expect.arrayContaining([
        { label: "Pricing expression", value: details.expression_pricing.expression },
        { label: "Applied pricing branches", value: "peak" },
        { label: "Request start for pricing", value: details.expression_pricing.started_at },
        { label: "Expression charge before discount", value: "$0.083 USD" },
        { label: "Expression settled charge", value: "$0.0415 USD" },
      ]),
    );
    expect(sections.find(({ title }) => title === "Evaluated pricing usage")?.rows).toContainEqual({
      label: "Expression usage · images",
      value: "2",
    });
    expect(sections.flatMap(({ rows }) => rows).map(({ label }) => label)).not.toContain("Input base price / 1M");
    const csv = ledgerEntriesCsv([
      {
        id: "charge",
        kind: "charge",
        amount: "-0.0415",
        balance_after: "1",
        request_id: "request",
        model: "fixture",
        created_at: details.expression_pricing.started_at,
        description: "",
        details,
      },
    ]);
    expect(csv).toContain('"Expression settled charge"');
    expect(csv).toContain('"$0.0415 USD"');
    expect(csv).toContain('"peak"');
  });

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
          { label: "Cache write tokens (total)", value: "2" },
          { label: "Cache write tokens · unspecified duration", value: "2" },
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
          { label: "Cache write default base price / 1M", value: "$6.25 USD" },
          { label: "Cache write default customer price / 1M", value: "$21.875 USD" },
        ],
      },
    ]);
  });

  it("keeps total writes separate from duration rows and applies the saved discount to each duration price", () => {
    const sections = ledgerReceiptSections({
      usage: {
        cache_write_tokens: 1000,
        cache_write_ttl_tokens: { "3600": 600, "300": 300 },
        cache_write_unclassified_tokens: 100,
      },
      price_snapshot: {
        source: "manual",
        discount: "0.5",
        cache_write_tiers: { "300": "0.0000025", "3600": "0.000004" },
      },
    });
    expect(sections[0].rows).toEqual([
      { label: "Cache write tokens (total)", value: "1000" },
      { label: "Cache write tokens · 5m", value: "300" },
      { label: "Cache write tokens · 1h", value: "600" },
      { label: "Cache write tokens · unspecified duration", value: "100" },
    ]);
    expect(sections[1].rows).toEqual(
      expect.arrayContaining([
        { label: "Cache write · 5m reference price / 1M", value: "$2.50 USD" },
        { label: "Cache write · 5m customer price / 1M", value: "$1.25 USD" },
        { label: "Cache write · 1h reference price / 1M", value: "$4.00 USD" },
        { label: "Cache write · 1h customer price / 1M", value: "$2.00 USD" },
      ]),
    );
  });

  it("displays server-saved category charges and exports duration columns without recomputing money", () => {
    const details = {
      usage: {
        cache_write_tokens: 10,
        cache_write_ttl_tokens: { "1800": 2, "43200": 8 },
        cache_write_unclassified_tokens: 0,
      },
      cost_breakdown: {
        input: "0.00000000001",
        cache_write_tiers: { "1800": "0.000012345678901234", "43200": "0.000098765432109876" },
        rounding_adjustment: "0.000000000879",
      },
    };
    const charges = ledgerReceiptSections(details).find(({ title }) => title === "Charge breakdown");
    expect(charges?.rows).toContainEqual({ label: "Cache write charge · 12h", value: "$0.000098765432109876 USD" });
    expect(charges?.rows).toContainEqual({ label: "Final rounding adjustment", value: "$0.000000000879 USD" });
    const csv = ledgerEntriesCsv([
      {
        id: "record",
        kind: "charge",
        amount: "-0.000111112",
        balance_after: "1",
        request_id: "request",
        model: "=unsafe()",
        created_at: "2026-01-01T00:00:00Z",
        description: "fixture",
        details,
      },
    ]);
    expect(csv).toContain('"Cache write tokens · 30m"');
    expect(csv).toContain('"Cache write tokens · 12h"');
    expect(csv).toContain('"Cache write charge · 12h"');
    expect(csv).toContain('"$0.000098765432109876 USD"');
    expect(csv).toContain('"\'=unsafe()"');
  });

  it.each(["0.0000025", "0"])(
    "shows the frozen default %s applied to an observed duration without its own price",
    (price) => {
      const sections = ledgerReceiptSections({
        usage: { cache_write_tokens: 10, cache_write_ttl_tokens: { "300": 2, "43200": 8 } },
        price_snapshot: {
          version: "token-v3",
          source: "manual",
          discount: "0.5",
          cache_creation_input_token_cost: price,
          cache_write_tiers: { "300": "0.000004" },
        },
      });
      const prices = sections.find(({ title }) => title === "Pricing snapshot");
      expect(prices?.rows).toEqual(
        expect.arrayContaining([
          { label: "Cache write · 5m reference price / 1M", value: "$4.00 USD" },
          {
            label: "Cache write · 12h (default) reference price / 1M",
            value: price === "0" ? "$0.00 USD" : "$2.50 USD",
          },
          {
            label: "Cache write · 12h (default) customer price / 1M",
            value: price === "0" ? "$0.00 USD" : "$1.25 USD",
          },
        ]),
      );
      expect(prices?.rows).not.toContainEqual(
        expect.objectContaining({ label: "Cache write · 5m (default) reference price / 1M" }),
      );
    },
  );

  it("does not apply the new default-duration policy to an older frozen price snapshot", () => {
    const sections = ledgerReceiptSections({
      usage: { cache_write_tokens: 2, cache_write_ttl_tokens: { "43200": 2 } },
      price_snapshot: {
        version: "token-v2",
        source: "manual",
        discount: "0.5",
        cache_creation_input_token_cost: "0.0000025",
        cache_write_tiers: { "300": "0.000004" },
      },
    });
    expect(sections.find(({ title }) => title === "Pricing snapshot")?.rows).not.toContainEqual(
      expect.objectContaining({ label: "Cache write · 12h (default) reference price / 1M" }),
    );
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
