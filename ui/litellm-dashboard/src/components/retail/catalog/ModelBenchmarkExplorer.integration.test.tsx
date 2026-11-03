import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ModelBenchmarkExplorer from "./ModelBenchmarkExplorer";
import type { CatalogModel } from "./types";

vi.mock("next/font/local", () => ({ default: () => ({ variable: "public-font" }) }));

vi.mock("./ModelBenchmarks", () => ({
  default: ({ models }: { models: CatalogModel[] }) => (
    <div data-testid="explorer-models">{models.map((model) => model.id).join(",")}</div>
  ),
}));

const model: CatalogModel = {
  id: "fixture-model",
  name: "Fixture model",
  provider: null,
  description: null,
  context_length: null,
  max_output_tokens: null,
  mode: "chat",
  input_modalities: [],
  output_modalities: [],
  metadata_source: null,
  available_to_me: false,
  offers: [],
  benchmarks: [
    {
      name: "Fixture evaluation",
      score: 35,
      metric: "index",
      source: "models_dev",
      source_url: "https://research.invalid/results",
      retrieved_at: "2026-01-01T00:00:00Z",
      version: null,
      harness: null,
      dataset: null,
      variant: null,
      evaluated_at: null,
      rank: null,
    },
  ],
  capabilities: {
    vision: null,
    tools: null,
    reasoning: null,
    prompt_caching: null,
    structured_output: null,
    audio_input: null,
    audio_output: null,
    pdf_input: null,
  },
};

describe("ModelBenchmarkExplorer", () => {
  it("opens the benchmark explorer for the supplied catalog models", async () => {
    const user = userEvent.setup();
    render(<ModelBenchmarkExplorer models={[model]} />);
    await user.click(screen.getByRole("button", { name: "Benchmarks" }));
    expect(screen.getByRole("dialog", { name: "Model benchmarks" })).toBeInTheDocument();
    expect(screen.getByTestId("explorer-models")).toHaveTextContent(model.id);
    expect(screen.getByText(/1 model currently shown/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Model benchmarks" })).not.toBeInTheDocument();
  });

  it("disables entry when the current set has no finite scored observations", () => {
    render(<ModelBenchmarkExplorer models={[{ ...model, benchmarks: [] }]} />);
    expect(screen.getByRole("button", { name: "Benchmarks" })).toBeDisabled();
  });

  it("passes only the current filtered subset after the catalog changes", async () => {
    const user = userEvent.setup();
    const second: CatalogModel = { ...model, id: "second-model" };
    const result = render(<ModelBenchmarkExplorer models={[model, second]} />);
    await user.click(screen.getByRole("button", { name: "Benchmarks" }));
    expect(screen.getByTestId("explorer-models")).toHaveTextContent(second.id);
    result.rerender(<ModelBenchmarkExplorer models={[model]} />);
    expect(screen.getByTestId("explorer-models")).toHaveTextContent(model.id);
    expect(screen.getByTestId("explorer-models")).not.toHaveTextContent(second.id);
  });
});
