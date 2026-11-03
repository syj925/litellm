import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, testQueryClient } from "../../../../tests/test-utils";
import { setServerRootPath } from "@/lib/serverRootPath";
import { retailRequest } from "../api";
import PublicModelCatalog from "./PublicModelCatalog";
import type { CatalogModel } from "./types";

vi.mock("../api", () => ({ retailRequest: vi.fn() }));
vi.mock("@/components/networking", () => ({ getProxyBaseUrl: () => "https://gateway.example/team-x" }));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authLoading: false }) }));

const model: CatalogModel = {
  id: "fixture-text-model",
  name: "Example model",
  provider: "openai",
  description: "A configured model for this test.",
  context_length: 12000,
  max_output_tokens: 2000,
  mode: "chat",
  input_modalities: ["text"],
  output_modalities: ["text"],
  capabilities: {
    vision: false,
    tools: true,
    reasoning: false,
    prompt_caching: true,
    structured_output: false,
    audio_input: false,
    audio_output: false,
    pdf_input: false,
  },
  metadata_source: "deployment",
  available_to_me: false,
  offers: [
    {
      pool_id: "fixture-pool",
      pool_name: "Public pool",
      source_kind: "openai",
      available_to_me: false,
      pricing: {
        currency: "USD",
        unit: "million_tokens",
        reference: { input: "2.5", output: "8", cache_read: "0.25", cache_write: null },
        customer: { input: "999", output: "999", cache_read: "999", cache_write: null },
        discount: "0.5",
        source_label: "Private account discount",
        version: 1,
        updated_at: null,
      },
    },
  ],
};

beforeEach(() => {
  testQueryClient.clear();
  vi.clearAllMocks();
  setServerRootPath("/team-x");
  vi.mocked(retailRequest).mockResolvedValue({ items: [model], metadata_note: "Published model reference rates." });
});

afterEach(() => {
  testQueryClient.clear();
  setServerRootPath("/");
});

describe("public pricing route", () => {
  it("loads published rates anonymously and defaults to a price table with real destinations", async () => {
    renderWithProviders(<PublicModelCatalog mode="pricing" />);

    expect(await screen.findByRole("heading", { name: "Pricing", exact: true })).toBeVisible();
    expect(retailRequest).toHaveBeenCalledWith("/public/model-catalog", null);
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getByRole("button", { name: "Table view" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("From $2.50")).toBeVisible();
    expect(screen.getByRole("link", { name: "Models", exact: true })).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/team-x\/ui\/models\/?$/),
    );
    expect(screen.getByRole("link", { name: "Developers", exact: true })).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/team-x\/ui\/developers\/?$/),
    );
    expect(screen.getByRole("link", { name: "Pricing", exact: true })).toHaveAttribute("aria-current", "page");
  });

  it("honors an explicit list view while keeping public rates independent of the requested price basis", async () => {
    renderWithProviders(<PublicModelCatalog mode="pricing" />, { searchParams: "?view=list&price=customer" });

    expect(await screen.findByRole("heading", { name: "Pricing", exact: true })).toBeVisible();
    expect(screen.getByRole("button", { name: "List view" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("From $2.50")).toBeVisible();
    expect(screen.queryByText(/999/)).not.toBeInTheDocument();
    expect(screen.queryByText("Private account discount")).not.toBeInTheDocument();
  });

  it("opens a selected model directly on its readable pricing details", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PublicModelCatalog mode="pricing" />);
    await user.click(await screen.findByRole("button", { name: /Example model/ }));

    expect(await screen.findByRole("tab", { name: "Pricing", exact: true })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "Model pricing" })).toBeVisible();
    expect(screen.getByText("$0.25")).toBeVisible();
    expect(screen.queryByText("Private account discount")).not.toBeInTheDocument();
  });

  it("keeps the original public models route in list mode", async () => {
    renderWithProviders(<PublicModelCatalog />);

    expect(await screen.findByRole("heading", { name: "Models", exact: true })).toBeVisible();
    expect(screen.getByRole("button", { name: "List view" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
