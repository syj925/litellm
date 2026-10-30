import { describe, expect, it } from "vitest";
import { formatContext, getModelIdentity } from "./modelIdentity";

describe("getModelIdentity", () => {
  it.each([
    ["claude-opus-4-5-20251101", "Claude Opus 4.5", "Anthropic", "anthropic"],
    ["anthropic/claude-haiku-5-5", "Claude Haiku 5.5", "Anthropic", "anthropic"],
    ["gpt-6.1-terra", "GPT-6.1 Terra", "OpenAI", "openai"],
    ["gemini/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite", "Google", "gemini"],
    ["o3-mini", "o3 mini", "OpenAI", "openai"],
  ])("formats the display identity for %s", (id, displayName, author, providerSlug) => {
    expect(getModelIdentity(id)).toEqual({ displayName, author, providerSlug });
  });

  it("preserves unknown IDs and supports an explicit model author", () => {
    expect(getModelIdentity("team/Quasar-XL-preview", "Research author")).toEqual({
      displayName: "team/Quasar-XL-preview",
      author: "Research author",
      providerSlug: "",
    });
    expect(getModelIdentity("custom-public-alias", "OpenAI")).toMatchObject({
      author: "OpenAI",
      providerSlug: "openai",
    });
    expect(getModelIdentity("llamafile-custom-20260101")).toEqual({
      displayName: "llamafile-custom-20260101",
      author: "Unknown author",
      providerSlug: "",
    });
  });

  it("does not mistake a resource transport for the model author", () => {
    expect(getModelIdentity("bedrock/claude-sonnet-4-5-20250929")).toMatchObject({
      author: "Anthropic",
      providerSlug: "anthropic",
    });
  });
});

describe("formatContext", () => {
  it.each([
    [undefined, "Not specified"],
    [null, "Not specified"],
    [0, "Not specified"],
    [-1, "Not specified"],
    [Number.NaN, "Not specified"],
    [Number.POSITIVE_INFINITY, "Not specified"],
    [512, "512"],
    [128_000, "128K"],
    [1_048_576, "1.05M"],
  ])("formats %s without inventing a context limit", (tokens, expected) => {
    expect(formatContext(tokens)).toBe(expected);
  });
});
