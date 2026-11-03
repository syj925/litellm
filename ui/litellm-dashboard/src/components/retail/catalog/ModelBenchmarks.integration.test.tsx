import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ModelBenchmarks, { benchmarkProtocol } from "./ModelBenchmarks";
import type { BenchmarkRecord, CatalogModel } from "./types";

vi.mock("next/font/local", () => ({ default: () => ({ variable: "public-font" }) }));

vi.mock("@/components/shared/charts", () => ({
  BarChart: ({ data }: { data: { label: string; Score: number }[] }) => (
    <div data-testid="benchmark-chart">{JSON.stringify(data)}</div>
  ),
}));

const benchmark: BenchmarkRecord = {
  name: "Fixture evaluation",
  score: 1450,
  metric: "Elo",
  source: "models_dev",
  source_url: "https://research.invalid/results",
  retrieved_at: "2026-01-01T00:00:00Z",
  version: "v2",
  harness: "Fixture harness",
  dataset: "held-out",
  variant: "high",
  evaluated_at: "2026-01-01",
  rank: 2,
};

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
  benchmarks: [benchmark],
  capabilities: {
    vision: true,
    tools: true,
    reasoning: true,
    prompt_caching: null,
    structured_output: null,
    audio_input: null,
    audio_output: null,
    pdf_input: null,
  },
};

describe("ModelBenchmarks", () => {
  it("plots raw scores with units and preserves source, date and source rank", () => {
    render(<ModelBenchmarks models={[model]} />);
    expect(screen.getByTestId("benchmark-chart")).toHaveTextContent('"Score":1450');
    expect(screen.getByRole("cell", { name: /1,450.*Elo/ })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "#2" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "2026-01-01" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "models.dev" })).toHaveAttribute("href", benchmark.source_url);
    expect(screen.getByText(/Raw scores are not a percentage/)).toBeInTheDocument();
    expect(screen.getByText(/Dataset: held-out/)).toBeInTheDocument();
    expect(screen.queryByText("1450%")).not.toBeInTheDocument();
  });

  it("keeps protocols and sources separate instead of averaging conflicting observations", async () => {
    const user = userEvent.setup();
    const second: BenchmarkRecord = { ...benchmark, source: "openrouter", score: 51.5 };
    render(<ModelBenchmarks models={[{ ...model, benchmarks: [benchmark, second] }]} />);
    expect(screen.getByTestId("benchmark-chart")).toHaveTextContent('"Score":1450');
    expect(screen.getByTestId("benchmark-chart")).not.toHaveTextContent('"Score":51.5');
    await user.click(screen.getByRole("combobox", { name: "Select benchmark" }));
    await user.click(screen.getByRole("option", { name: /OpenRouter/ }));
    expect(screen.getByTestId("benchmark-chart")).toHaveTextContent('"Score":51.5');
    expect(screen.getByTestId("benchmark-chart")).not.toHaveTextContent('"Score":1450');
  });

  it("distinguishes version, harness, dataset and reasoning setting in the comparison key", () => {
    const baseline = benchmarkProtocol(benchmark);
    for (const field of ["version", "harness", "dataset", "variant", "metric"] as const) {
      expect(benchmarkProtocol({ ...benchmark, [field]: "different" })).not.toBe(baseline);
    }
  });

  it("does not turn capability flags into benchmark scores when evaluations are missing", () => {
    render(<ModelBenchmarks models={[{ ...model, benchmarks: [] }]} />);
    expect(screen.getByText(/No sourced benchmark scores/)).toBeInTheDocument();
    expect(screen.queryByTestId("benchmark-chart")).not.toBeInTheDocument();
  });

  it("retains duplicate source observations without inventing an average", () => {
    render(
      <ModelBenchmarks
        models={[
          {
            ...model,
            benchmarks: [
              { ...benchmark, score: 10 },
              { ...benchmark, score: 12 },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByTestId("benchmark-chart")).toHaveTextContent('"Score":10');
    expect(screen.getByTestId("benchmark-chart")).toHaveTextContent('"Score":12');
    expect(screen.getByTestId("benchmark-chart")).not.toHaveTextContent('"Score":11');
  });

  it("does not render unsafe source URLs as links", () => {
    render(
      <ModelBenchmarks models={[{ ...model, benchmarks: [{ ...benchmark, source_url: "javascript:alert(1)" }] }]} />,
    );
    expect(screen.queryByRole("link", { name: "models.dev" })).not.toBeInTheDocument();
  });

  it("retains unlabelled raw observations without plotting an unknown metric scale", () => {
    render(<ModelBenchmarks models={[{ ...model, benchmarks: [{ ...benchmark, metric: null }] }]} />);
    expect(screen.getByText(/has not supplied score units/)).toBeInTheDocument();
    expect(screen.queryByTestId("benchmark-chart")).not.toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "1,450" })).toBeInTheDocument();
  });
});
