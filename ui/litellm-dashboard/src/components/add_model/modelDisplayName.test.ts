import { describe, expect, it } from "vitest";
import { Providers } from "../provider_info_helpers";
import { buildModelChoices, modelDisplayName } from "./modelDisplayName";

describe("modelDisplayName", () => {
  it.each([
    ["Anthropic", "anthropic/claude-test", "claude-test"],
    ["OpenAI", "openai/gpt-test", "gpt-test"],
    ["Google_AI_Studio", "gemini/gemini-test", "gemini-test"],
    [Providers.Google_AI_Studio, "gemini/gemini-test", "gemini-test"],
    ["Vertex_AI", "vertex_ai/claude-test", "claude-test"],
    [Providers.Vertex_AI, "vertex_ai/gemini-test", "gemini-test"],
    ["Bedrock", "bedrock/anthropic.claude-test-v1:0", "anthropic.claude-test-v1:0"],
    [Providers.Bedrock, "bedrock/us.anthropic.claude-test-v1:0", "us.anthropic.claude-test-v1:0"],
    ["anthropic", "anthropic/claude-test", "claude-test"],
    [Providers.OpenAI_Compatible, "openai/vendor/custom-model", "vendor/custom-model"],
    [Providers.Openrouter, "openrouter/anthropic/claude-test", "anthropic/claude-test"],
    [Providers.Anthropic, "anthropic/anthropic/claude-test", "anthropic/claude-test"],
  ])("shows the upstream name for %s without changing its namespace", (provider, stored, visible) => {
    expect(modelDisplayName(stored, provider)).toBe(visible);
  });

  it.each([
    [Providers.Anthropic, "openai/claude-test"],
    [Providers.OpenAI, "vendor/model-name"],
    ["Custom Provider", "custom/model-name"],
    [null, "anthropic/claude-test"],
    [undefined, "anthropic/claude-test"],
    [Providers.Anthropic, "claude-test"],
    [Providers.Anthropic, "anthropic/"],
    [Providers.Anthropic, ""],
  ])("keeps unqualified or unrelated model IDs intact", (provider, stored) => {
    expect(modelDisplayName(stored, provider)).toBe(stored);
  });
});

describe("buildModelChoices", () => {
  it("prefers an exact selected value over equivalent bare and prefixed catalog entries", () => {
    const catalog = ["claude-haiku", "anthropic/claude-haiku", "claude-opus"];
    const selected = ["anthropic/claude-haiku"];
    expect(buildModelChoices(catalog, selected, Providers.Anthropic)).toEqual({
      options: [
        { value: "anthropic/claude-haiku", label: "claude-haiku" },
        { value: "claude-opus", label: "claude-opus" },
      ],
      selectAllValues: ["anthropic/claude-haiku", "claude-opus"],
      catalogCount: 2,
    });
    expect(catalog).toEqual(["claude-haiku", "anthropic/claude-haiku", "claude-opus"]);
    expect(selected).toEqual(["anthropic/claude-haiku"]);
  });

  it("keeps distinct already-selected targets even when their display names agree", () => {
    expect(
      buildModelChoices(
        ["claude-haiku", "claude-opus"],
        ["anthropic/claude-haiku", "claude-haiku", "ad-hoc"],
        Providers.Anthropic,
      ),
    ).toEqual({
      options: [
        { value: "anthropic/claude-haiku", label: "claude-haiku" },
        { value: "claude-haiku", label: "claude-haiku" },
        { value: "ad-hoc", label: "ad-hoc" },
        { value: "claude-opus", label: "claude-opus" },
      ],
      selectAllValues: ["anthropic/claude-haiku", "claude-haiku", "ad-hoc", "claude-opus"],
      catalogCount: 2,
    });
  });

  it("deduplicates only one adapter prefix and preserves deeper or differently-cased model IDs", () => {
    const result = buildModelChoices(
      ["openai/vendor/model", "vendor/model", "model", "openai/Model", "other/model"],
      ["openai/vendor/model"],
      Providers.OpenAI,
    );
    expect(result.catalogCount).toBe(4);
    expect(result.selectAllValues).toEqual(["openai/vendor/model", "model", "openai/Model", "other/model"]);
  });

  it("restores the original catalog value after its selected alternative is removed", () => {
    expect(buildModelChoices(["claude-haiku"], [], Providers.Anthropic)).toEqual({
      options: [{ value: "claude-haiku", label: "claude-haiku" }],
      selectAllValues: ["claude-haiku"],
      catalogCount: 1,
    });
  });
});
