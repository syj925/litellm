import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import CatalogCompare from "./CatalogCompare";
import type { CatalogModel } from "./types";

vi.mock("next/font/local", () => ({ default: () => ({ variable: "public-font" }) }));

const available: CatalogModel = {
  id: "claude-sonnet-4-5-20250929",
  name: "claude-sonnet-4-5-20250929",
  provider: "anthropic",
  description: null,
  context_length: 128_000,
  max_output_tokens: null,
  mode: "chat",
  input_modalities: ["text", "image"],
  output_modalities: ["text"],
  metadata_source: "reference_catalog",
  available_to_me: true,
  capabilities: {
    vision: true,
    tools: true,
    reasoning: null,
    prompt_caching: false,
    structured_output: null,
    audio_input: null,
    audio_output: null,
    pdf_input: null,
  },
  offers: [
    {
      pool_id: "fixture-pool",
      pool_name: "Fixture pool",
      source_kind: "anthropic",
      available_to_me: true,
      pricing: {
        currency: "USD",
        unit: "million_tokens",
        reference: { input: "10", output: "20", cache_read: null, cache_write: null },
        customer: { input: "2", output: "4", cache_read: null, cache_write: null },
        discount: "0.2",
        source_label: null,
        version: null,
        updated_at: null,
      },
    },
  ],
};

const unlisted: CatalogModel = {
  ...available,
  id: "custom/fixture-silver",
  name: "Fixture silver",
  provider: null,
  context_length: null,
  input_modalities: [],
  output_modalities: [],
  available_to_me: false,
  capabilities: { ...available.capabilities, tools: null, vision: null },
  offers: [],
};

describe("CatalogCompare", () => {
  it("keeps generic cache prices for flat models while tiered models show their duration prices", () => {
    const tiered: CatalogModel = {
      ...available,
      offers: [],
      reference_pricing: { input: "1", output: "2", cache_read: "0.1", cache_write: "99", cache_write_tiers: { "300": "2.5", "3600": "2.5" } },
    };
    const flat: CatalogModel = {
      ...unlisted,
      reference_pricing: { input: "1", output: "2", cache_read: "0.1", cache_write: "3.33" },
    };
    render(<CatalogCompare models={[tiered, flat]} open onOpenChange={vi.fn()} basis="reference" publicView />);
    const generic = screen.getByRole("row", { name: /Reference cache write \/ 1M tokens/ });
    expect(within(generic).getByText("From $3.33")).toBeInTheDocument();
    expect(within(generic).getByText("See duration rates")).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /Reference cache write · 5m/ })).toHaveTextContent("From $2.50");
    expect(screen.getByRole("row", { name: /Reference cache write · 1h/ })).toHaveTextContent("From $2.50");
    expect(screen.queryByText("From $99.00")).not.toBeInTheDocument();
  });

  it("keeps anonymous comparisons public even when the supplied model contains account-specific fields", () => {
    render(<CatalogCompare models={[available]} open onOpenChange={vi.fn()} basis="customer" publicView />);
    const dialog = screen.getByRole("dialog", { name: "Compare models" });
    expect(within(dialog).getByRole("row", { name: /Reference input/ })).toBeInTheDocument();
    expect(within(dialog).getByText("From $10.00")).toBeInTheDocument();
    expect(within(dialog).getByRole("row", { name: /Tool calling Supported/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("row", { name: /Your access|Your input|Your output/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByText("From $2.00")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("Available")).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/customer prices/i)).not.toBeInTheDocument();
  });

  it("compares access, specifications and the two price bases without inventing unknown values", () => {
    render(<CatalogCompare models={[available, unlisted]} open onOpenChange={vi.fn()} basis="customer" />);
    const dialog = screen.getByRole("dialog", { name: "Compare models" });
    expect(within(dialog).getByRole("columnheader", { name: /Claude Sonnet 4.5/ })).toBeInTheDocument();
    expect(within(dialog).getByText(available.id)).toBeInTheDocument();
    expect(within(dialog).getByText(unlisted.id)).toBeInTheDocument();
    const context = within(dialog).getByRole("row", { name: /Input context window/ });
    expect(within(context).getByText("128K")).toBeInTheDocument();
    expect(within(context).getByText("Not specified")).toBeInTheDocument();
    expect(within(dialog).getByText("Access required")).toBeInTheDocument();
    const reference = within(dialog).getByRole("row", { name: /Reference input/ });
    expect(within(reference).getByText("From $10.00")).toBeInTheDocument();
    const customer = within(dialog).getByRole("row", { name: /Your input/ });
    expect(within(customer).getByText("From $2.00")).toBeInTheDocument();
    expect(within(customer).getByText("Not listed")).toBeInTheDocument();
    expect(within(customer).queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("copies the full request ID and closes the comparison", async () => {
    const user = userEvent.setup();
    const clipboard = vi.spyOn(navigator.clipboard, "writeText");
    const onOpenChange = vi.fn();
    render(<CatalogCompare models={[available]} open onOpenChange={onOpenChange} basis="reference" />);
    await user.click(screen.getByRole("button", { name: `Copy model ID ${available.id}` }));
    expect(clipboard).toHaveBeenCalledWith(available.id);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false, expect.anything());
  });

  it("charts exact token limits on a shared scale for each metric and keeps missing limits unknown", () => {
    const large: CatalogModel = { ...available, id: "fixture-large", name: "Fixture large", context_window: 200000, context_length: 120000 };
    const small: CatalogModel = { ...unlisted, id: "fixture-small", name: "Fixture small", context_window: 100000, context_length: 60000 };
    render(<CatalogCompare models={[large, small]} open onOpenChange={vi.fn()} basis="reference" publicView />);
    const context = screen.getByRole("figure", { name: "Total context window comparison" });
    const largeMeter = within(context).getByRole("meter", { name: "Fixture large Total context window" });
    const smallMeter = within(context).getByRole("meter", { name: "Fixture small Total context window" });
    expect(largeMeter).toHaveAttribute("aria-valuenow", String(large.context_window));
    expect(smallMeter).toHaveAttribute("aria-valuemax", String(large.context_window));
    expect(smallMeter.firstElementChild).toHaveStyle({ width: "50%" });
    const output = screen.getByRole("figure", { name: "Maximum output comparison" });
    expect(within(output).queryByRole("meter")).not.toBeInTheDocument();
    expect(within(output).getAllByText("Not specified")).toHaveLength(2);
    expect(screen.getByText(/these are not quality scores/i)).toBeInTheDocument();
  });
});
