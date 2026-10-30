import { useState } from "react";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../../tests/test-utils";
import CatalogModelDetail from "./CatalogModelDetail";
import { getModelIdentity } from "./modelIdentity";
import type { CatalogDetailTab, CatalogModel } from "./types";

const model: CatalogModel = {
  id: "claude-fixture-1-2",
  name: "claude-fixture-1-2",
  provider: "anthropic",
  description: "A description published by the site.",
  context_length: null,
  max_output_tokens: null,
  mode: "chat",
  input_modalities: [],
  output_modalities: [],
  capabilities: {
    tools: true,
    vision: false,
    reasoning: null,
    prompt_caching: null,
    structured_output: null,
    audio_input: null,
    audio_output: null,
    pdf_input: null,
  },
  metadata_source: "deployment",
  available_to_me: true,
  offers: [
    {
      pool_id: "aws-fixture",
      pool_name: "AWS fixture pool",
      source_kind: "bedrock",
      available_to_me: true,
      pricing: {
        currency: "USD",
        unit: "million_tokens",
        reference: { input: "12", output: "34", cache_read: null, cache_write: null },
        customer: { input: "6", output: "17", cache_read: null, cache_write: null },
        discount: "0.5",
        source_label: "Customer pool discount",
        version: 1,
        updated_at: null,
      },
    },
    {
      pool_id: "an-fixture",
      pool_name: "Anthropic fixture pool",
      source_kind: "anthropic",
      available_to_me: false,
      pricing: {
        currency: "USD",
        unit: "million_tokens",
        reference: { input: "14", output: "35", cache_read: null, cache_write: null },
        customer: { input: "999", output: "999", cache_read: null, cache_write: null },
        discount: "0.1",
        source_label: "Must stay private",
        version: 1,
        updated_at: null,
      },
    },
  ],
};

function Detail({
  initialTab = "overview",
  data = model,
  publicView = false,
}: {
  initialTab?: CatalogDetailTab;
  data?: CatalogModel;
  publicView?: boolean;
}) {
  const [tab, setTab] = useState<CatalogDetailTab>(initialTab);
  return (
    <CatalogModelDetail
      model={data}
      activeTab={tab}
      onTabChange={setTab}
      onBack={vi.fn()}
      apiBaseUrl="https://gateway.example"
      publicView={publicView}
    />
  );
}

describe("catalog model details", () => {
  it("prioritizes integration actions and keeps source attribution collapsed until requested", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Detail publicView />);
    const integration = screen.getByRole("region", { name: "Integrate this model" });
    expect(within(integration).getByText(model.id)).toBeInTheDocument();
    expect(within(integration).getByText("OpenAI compatible")).toBeInTheDocument();
    expect(within(integration).getByText("Anthropic Messages")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Data sources" })).not.toBeVisible();
    await user.click(screen.getByText("View data sources"));
    expect(screen.getByRole("heading", { name: "Data sources" })).toBeVisible();
    await user.click(within(integration).getByRole("button", { name: "Copy request model ID" }));
    expect(await navigator.clipboard.readText()).toBe(model.id);
    await user.click(within(integration).getByRole("button", { name: "View API examples" }));
    expect(screen.getByRole("tab", { name: "API" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "Request code" })).toHaveTextContent(JSON.stringify(model.id));
  });

  it("opens the model's resource pools from the overview without granting access", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Detail data={{ ...model, available_to_me: false }} />);
    const integration = screen.getByRole("region", { name: "Integrate this model" });
    expect(within(integration).getByText(/Resource pool access is required/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Open playground/ })).not.toBeInTheDocument();
    await user.click(within(integration).getByRole("button", { name: "View resource pools" }));
    expect(screen.getByRole("tab", { name: /Resources/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(model.offers[0].pool_name)).toBeInTheDocument();
  });

  it("keeps unpublished resources and unknown API formats explicit in the integration card", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Detail data={{ ...model, mode: null, offers: [], available_to_me: false }} publicView />);
    const integration = screen.getByRole("region", { name: "Integrate this model" });
    expect(within(integration).getByText(/No resource pool is published/)).toBeInTheDocument();
    expect(within(integration).queryByText("API example formats")).not.toBeInTheDocument();
    expect(within(integration).queryByRole("button", { name: "View resource pools" })).not.toBeInTheDocument();
    await user.click(within(integration).getByRole("button", { name: "View integration reference" }));
    expect(screen.getByText(/A request format has not been published/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Request code" })).not.toBeInTheDocument();
  });

  it("keeps the public introduction separate from personal availability and pricing", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Detail publicView />);
    expect(screen.queryByText("Available to you")).not.toBeInTheDocument();
    expect(screen.queryByText("Access required")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Manage API keys" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in to use" })).toHaveAttribute("href", "/ui/login");
    await user.click(screen.getByRole("tab", { name: /Resources/ }));
    expect(screen.getByText(model.offers[0].pool_name)).toBeInTheDocument();
    expect(screen.queryByText("Available to you")).not.toBeInTheDocument();
    expect(screen.queryByText("Access required")).not.toBeInTheDocument();
    expect(screen.queryByText("Contact your administrator for access")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Pricing" }));
    const aws = screen.getByRole("table", { name: "Pricing for AWS fixture pool" });
    expect(within(aws).getByText("$12.00")).toBeInTheDocument();
    expect(within(aws).queryByRole("columnheader", { name: "Your price / 1M tokens" })).not.toBeInTheDocument();
    expect(within(aws).queryByText("$6.00")).not.toBeInTheDocument();
    expect(screen.queryByText(/50% of reference price/)).not.toBeInTheDocument();
    expect(screen.queryByText("Must stay private")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "API" }));
    expect(screen.getByRole("region", { name: "Request code" })).toHaveTextContent("LITELLM_API_KEY");
    expect(screen.queryByText(/model invocation requires access/)).not.toBeInTheDocument();
  });

  it("shows model identity, published specifications, and distinct unknown capabilities", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Detail />);
    expect(
      screen.getByRole("heading", { name: getModelIdentity(model.id, model.provider).displayName }),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Anthropic logo" })).toBeInTheDocument();
    expect(screen.getByText(model.description!)).toBeInTheDocument();
    const capabilities = screen.getByRole("region", { name: "Capabilities" });
    expect(within(capabilities).getByText("Supported")).toBeInTheDocument();
    expect(within(capabilities).getByText("Not supported")).toBeInTheDocument();
    expect(within(capabilities).getAllByText("Not specified")).toHaveLength(6);
    await user.click(screen.getByRole("tab", { name: /Resources/ }));
    expect(screen.getByText("Contact your administrator for access")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Manage keys" })).toHaveAttribute("href", "/ui/api-keys");
  });

  it("keeps customer quotes tied to the resource pool and hides unauthorized customer prices", () => {
    renderWithProviders(<Detail initialTab="pricing" />);
    const aws = screen.getByRole("table", { name: "Pricing for AWS fixture pool" });
    expect(within(aws).getByText("$12.00")).toBeInTheDocument();
    expect(within(aws).getByText("$6.00")).toBeInTheDocument();
    const unauthorized = screen.getByRole("table", { name: "Pricing for Anthropic fixture pool" });
    expect(within(unauthorized).getByText("$14.00")).toBeInTheDocument();
    expect(within(unauthorized).queryByText("$999.00")).not.toBeInTheDocument();
    expect(screen.queryByText("Must stay private")).not.toBeInTheDocument();
    expect(screen.getByText(/50% of reference price/)).toBeInTheDocument();
    expect(screen.getAllByText("Not listed").length).toBeGreaterThan(0);
  });

  it("switches API languages without changing the public request ID and can copy that ID", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Detail initialTab="api" />);
    expect(screen.getByRole("region", { name: "Request code" })).toHaveTextContent("/v1/chat/completions");
    await user.click(screen.getByRole("button", { name: "Python" }));
    expect(screen.getByRole("region", { name: "Request code" })).toHaveTextContent("client.chat.completions.create");
    expect(screen.getByRole("region", { name: "Request code" })).toHaveTextContent(JSON.stringify(model.id));
    await user.click(screen.getAllByRole("button", { name: "Copy model ID" })[0]);
    expect(await navigator.clipboard.readText()).toBe(model.id);
  });

  it("allows an unavailable model's introduction but does not claim it can be invoked", () => {
    renderWithProviders(
      <Detail initialTab="api" data={{ ...model, available_to_me: false, mode: null, offers: [] }} />,
    );
    expect(screen.queryByRole("link", { name: "Open playground" })).not.toBeInTheDocument();
    expect(screen.getByText(/model invocation requires access/)).toBeInTheDocument();
    expect(screen.getByText(/A request format has not been published/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Request code" })).not.toBeInTheDocument();
  });
});
