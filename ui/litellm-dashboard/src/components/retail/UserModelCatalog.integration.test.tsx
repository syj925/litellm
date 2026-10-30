import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../tests/test-utils";
import UserModelCatalog from "./UserModelCatalog";
import ModelCatalogBrowser from "./catalog/ModelCatalogBrowser";
import type { CatalogModel } from "./catalog/types";

vi.mock("@/components/networking", () => ({
  getProxyBaseUrl: () => "",
  getGlobalLitellmHeaderName: () => "Authorization",
}));
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => ({ accessToken: "test-session", userId: "self" }),
}));

const model: CatalogModel = {
  id: "claude-alpha",
  name: "claude-alpha",
  provider: "anthropic",
  description: "Published fixture description.",
  context_length: 200000,
  max_output_tokens: 10000,
  mode: "chat",
  input_modalities: ["text", "image"],
  output_modalities: ["text"],
  capabilities: {
    vision: true,
    tools: true,
    reasoning: null,
    prompt_caching: true,
    structured_output: null,
    audio_input: null,
    audio_output: null,
    pdf_input: null,
  },
  metadata_source: "reference_catalog",
  available_to_me: true,
  offers: [
    {
      pool_id: "aws-fixture",
      pool_name: "AWS fixture",
      source_kind: "bedrock",
      available_to_me: true,
      pricing: {
        currency: "USD",
        unit: "million_tokens",
        reference: { input: "10", output: "20", cache_read: null, cache_write: null },
        customer: { input: "5", output: "10", cache_read: null, cache_write: null },
        discount: "0.5",
        source_label: "Fixture discount",
        version: 1,
        updated_at: null,
      },
    },
  ],
};
const publicModels: CatalogModel[] = [
  model,
  { ...model, id: "gpt-beta", name: "gpt-beta", provider: "openai", available_to_me: false },
];

afterEach(() => {
  testQueryClient.clear();
  vi.unstubAllGlobals();
});

function setup(items: CatalogModel[], publicView = false, searchParams = "", status = 200) {
  const request = vi
    .fn()
    .mockImplementation(
      async () =>
        new Response(
          JSON.stringify(
            status === 200
              ? { items, metadata_note: "Published reference specifications." }
              : { detail: "Catalog unavailable" },
          ),
          { status },
        ),
    );
  vi.stubGlobal("fetch", request);
  renderWithProviders(publicView ? <ModelCatalogBrowser publicView /> : <UserModelCatalog />, { searchParams });
  return request;
}

it("loads the anonymous catalog without sending an authorization token or showing personal prices", async () => {
  const request = setup(publicModels, true, "?price=customer&availability=available");
  expect(await screen.findByRole("button", { name: "Claude Alpha" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "GPT-Beta" })).toBeInTheDocument();
  expect(screen.queryByText("Available to you")).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Pricing display" })).not.toBeInTheDocument();
  expect(screen.queryByText("$5.00")).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Sort models" })).toHaveTextContent("Name A–Z");
  expect(screen.getByRole("combobox", { name: "Minimum context length" })).toHaveTextContent("Any context");
  expect(screen.getByRole("combobox", { name: "Maximum input price" })).toHaveTextContent("Any price");
  expect(
    request.mock.calls.every(
      ([url, options]) => url === "/retail/public/model-catalog" && !new Headers(options.headers).has("Authorization"),
    ),
  ).toBe(true);
});

it("uses only the personal endpoint in the sidebar page and opens the authorized pool pricing", async () => {
  const request = setup([model], false, "?model=claude-alpha&model_tab=pricing");
  const priceTable = await screen.findByRole("table", { name: "Pricing for AWS fixture" });
  expect(within(priceTable).getByText("$5.00")).toBeInTheDocument();
  expect(screen.queryByText("GPT-Beta")).not.toBeInTheDocument();
  expect(
    request.mock.calls.every(
      ([url, options]) =>
        url === "/retail/model-catalog" && new Headers(options.headers).get("Authorization") === "Bearer test-session",
    ),
  ).toBe(true);
});

it("keeps search state when opening a model and returning to the list", async () => {
  setup(publicModels, true);
  await screen.findByRole("button", { name: "Claude Alpha" });
  fireEvent.change(screen.getByRole("textbox", { name: "Search models" }), { target: { value: "claude" } });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Alpha" }));
  expect(await screen.findByRole("heading", { name: "Claude Alpha" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Back to models" }));
  expect(await screen.findByRole("textbox", { name: "Search models" })).toHaveValue("claude");
  expect(screen.queryByRole("button", { name: "GPT-Beta" })).not.toBeInTheDocument();
});

it("wires selection into anonymous model comparison without account-specific rows", async () => {
  setup(publicModels, true);
  await screen.findByRole("button", { name: "Claude Alpha" });
  await userEvent.click(screen.getByRole("checkbox", { name: "Compare claude-alpha" }));
  await userEvent.click(screen.getByRole("checkbox", { name: "Compare gpt-beta" }));
  await userEvent.click(screen.getByRole("button", { name: "Compare (2)" }));
  const dialog = await screen.findByRole("dialog", { name: "Compare models" });
  expect(within(dialog).getByText("Input context window")).toBeInTheDocument();
  expect(within(dialog).queryByText("Your access")).not.toBeInTheDocument();
  expect(within(dialog).queryByText("Your input / 1M tokens")).not.toBeInTheDocument();
});

it("does not fall back to administrative model data when the catalog fails", async () => {
  const request = setup([], false, "", 403);
  expect(await screen.findByText("Unable to load model catalog")).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(1);
});
