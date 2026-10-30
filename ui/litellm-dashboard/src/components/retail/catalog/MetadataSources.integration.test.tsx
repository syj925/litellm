import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MetadataSourceDetails, MetadataSourceSummary } from "./MetadataSources";
import { catalogDescription } from "./catalogState";
import type { CatalogModel } from "./types";

const model: CatalogModel = {
  id: "fixture-model",
  name: "fixture-model",
  provider: null,
  description: null,
  context_length: null,
  max_output_tokens: null,
  mode: null,
  input_modalities: [],
  output_modalities: [],
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
  metadata_source: null,
  available_to_me: false,
  offers: [],
};

describe("catalog source attribution", () => {
  it("displays both sources and the field attribution without implying channel verification", () => {
    render(
      <MetadataSourceDetails
        model={{
          ...model,
          metadata_sources: [
            {
              source: "models_dev",
              url: "https://models.dev/models/fixture",
              retrieved_at: "2026-01-01T00:00:00Z",
              fields: ["context_window", "input_modalities"],
              model_id: "lab/fixture",
            },
            {
              source: "openrouter",
              url: "https://openrouter.ai/lab/fixture",
              retrieved_at: "2026-01-01T00:00:00Z",
              fields: ["description"],
              model_id: "lab/fixture",
            },
          ],
          metadata_conflicts: ["context_window"],
        }}
      />,
    );
    expect(screen.getByRole("link", { name: "models.dev" })).toHaveAttribute(
      "href",
      "https://models.dev/models/fixture",
    );
    expect(screen.getByRole("link", { name: "OpenRouter" })).toBeInTheDocument();
    expect(screen.getByText("Context window, Input modalities")).toBeInTheDocument();
    expect(screen.getByText(/Sources disagree on Context window/)).toBeInTheDocument();
    expect(screen.getByText(/Capabilities have not been verified on every channel/)).toBeInTheDocument();
  });

  it("keeps supplied markup as text and never makes an unsafe source URL navigable", () => {
    render(
      <MetadataSourceDetails
        model={{
          ...model,
          metadata_sources: [
            {
              source: "openrouter",
              url: "javascript:alert(1)",
              retrieved_at: null,
              fields: ["<script>untrusted</script>"],
              model_id: null,
            },
          ],
        }}
      />,
    );
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("<script>untrusted</script>")).toBeInTheDocument();
  });

  it("attributes the legacy catalog and leaves unpublished descriptions explicit", () => {
    render(<MetadataSourceSummary model={{ ...model, metadata_source: "reference_catalog" }} />);
    expect(screen.getByText(/Sources: LiteLLM reference catalog/)).toBeInTheDocument();
    expect(catalogDescription(model)).toBe("A model description has not been published.");
    expect(catalogDescription({ ...model, description: "Supplied model overview" })).toBe("Supplied model overview");
  });
});
