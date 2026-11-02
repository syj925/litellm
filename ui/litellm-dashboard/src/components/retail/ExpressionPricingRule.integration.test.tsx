import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ExpressionPricingRule from "./ExpressionPricingRule";
import type { ExpressionPolicy } from "./expressionPricing";
import type { ExpressionPriceDisplay } from "./priceDisplay";

const policy: ExpressionPolicy = {
  version: "expression-v1",
  expression: "len > 200000 ? p*4+c*16 : p*2+c*8",
  timezone: "UTC",
  usage_bounds: {},
};
const display: ExpressionPriceDisplay = {
  status: "complete",
  token_rates: [
    {
      conditions: ["Standard", "Input context ≤ 200,000 tokens"],
      rates: { p: "2", c: "8", cr: "0.2" },
      baseline: true,
    },
    {
      conditions: ["Standard", "Input context > 200,000 tokens"],
      rates: { p: "4", c: "16", cr: "0.4" },
      baseline: false,
    },
  ],
  fees: [{ conditions: [], label: "Web search", amount: "0.03", usage_key: "web_search_requests", unit: "query" }],
  modifiers: [{ conditions: ["US processing"], multiplier: "1.1", applies_to: "Token charges" }],
  notes: ["Upstream project free allowances are not allocated to customers."],
};

describe("readable pricing", () => {
  it("shows context prices, tool units and adjustments while keeping the formula collapsed", () => {
    render(<ExpressionPricingRule policy={policy} display={display} />);
    const table = screen.getByRole("table", { name: "Token prices by condition" });
    expect(within(table).getByRole("row", { name: /Standard · Input context ≤/ })).toHaveTextContent("$2.00$8.00$0.20");
    expect(within(table).getByRole("row", { name: /Standard · Input context >/ })).toHaveTextContent(
      "$4.00$16.00$0.40",
    );
    expect(screen.getByRole("table", { name: "Additional charges" })).toHaveTextContent(
      "Web searchWhen used$0.03 per query",
    );
    expect(screen.getByText("US processing")).toBeVisible();
    expect(screen.getByText(policy.expression)).not.toBeVisible();
    fireEvent.click(screen.getByText("Technical details"));
    expect(screen.getByText(policy.expression)).toBeVisible();
  });

  it("does not offer numerical quotations when the backend cannot fully describe a custom rule", () => {
    render(
      <ExpressionPricingRule
        policy={policy}
        display={{
          status: "unsupported",
          token_rates: [],
          fees: [],
          modifiers: [],
          notes: ["Usage-dependent output rate"],
        }}
      />,
    );
    expect(screen.getByText("Request-dependent price")).toBeVisible();
    expect(screen.getByText("Usage-dependent output rate")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText(policy.expression)).not.toBeVisible();
  });
});
