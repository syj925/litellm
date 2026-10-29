import { describe, expect, it } from "vitest";
import { filterModelIds, mergeUpstreamModels, uniqueModelIds } from "./upstreamModelSelection";

describe("upstream model selection", () => {
  it("deduplicates model ids and filters the whole list case insensitively", () => {
    const models = uniqueModelIds([" alpha ", "beta", "alpha", "", "ALPHA-2"]);
    expect(models).toEqual(["alpha", "beta", "ALPHA-2"]);
    expect(filterModelIds(models, "  ALPHA  ")).toEqual(["alpha", "ALPHA-2"]);
  });

  it("keeps edited public mappings while adding selected upstream ids", () => {
    const result = mergeUpstreamModels(
      ["openai/model-a"],
      [{ public_name: "customer-name", litellm_model: "openai/model-a" }],
      ["model-a", "model-b"],
      "openai",
    );
    expect(result).toEqual({
      model: ["openai/model-a", "openai/model-b"],
      model_mappings: [
        { public_name: "customer-name", litellm_model: "openai/model-a" },
        { public_name: "model-b", litellm_model: "openai/model-b" },
      ],
    });
  });

  it("preserves namespace ids and adds only one routing prefix across repeated imports", () => {
    const first = mergeUpstreamModels([], [], ["anthropic/model-a", "anthropic/model-a"], "openrouter");
    const repeated = mergeUpstreamModels(first.model, first.model_mappings, ["anthropic/model-a"], "openrouter");
    expect(repeated).toEqual(first);
    expect(first.model_mappings).toEqual([
      { public_name: "anthropic/model-a", litellm_model: "openrouter/anthropic/model-a" },
    ]);
  });

  it("does not swallow an upstream namespace which has the same name as the transport provider", () => {
    expect(mergeUpstreamModels([], [], ["openai/model-a"], "openai").model_mappings).toEqual([
      { public_name: "openai/model-a", litellm_model: "openai/openai/model-a" },
    ]);
  });

  it("removes wildcard and custom sentinels when importing concrete deployments", () => {
    const result = mergeUpstreamModels(
      ["all-wildcard", "custom"],
      [{ public_name: "custom", litellm_model: "custom" }],
      ["model-a"],
      "gemini",
    );
    expect(result.model).toEqual(["gemini/model-a"]);
  });

  it("keeps a manually entered model which has not reached the mapping field yet", () => {
    const result = mergeUpstreamModels("manual-model", [], ["new-model"], "anthropic");
    expect(result.model_mappings).toEqual([
      { public_name: "manual-model", litellm_model: "manual-model" },
      { public_name: "new-model", litellm_model: "anthropic/new-model" },
    ]);
  });

  it("does not duplicate a raw selection that already has an edited prefixed mapping", () => {
    expect(
      mergeUpstreamModels(
        ["model-a"],
        [{ public_name: "edited", litellm_model: "openai/model-a" }],
        ["model-a"],
        "openai",
      ),
    ).toEqual({
      model: ["openai/model-a"],
      model_mappings: [{ public_name: "edited", litellm_model: "openai/model-a" }],
    });
  });
});
