import { describe, expect, it } from "vitest";
import { catalogApiExample, catalogApiProtocols } from "./catalogApiExamples";
import type { CatalogModel } from "./types";

const model = {
  id: "claude-fixture-1-2",
  provider: "anthropic",
  mode: "chat",
} as CatalogModel;

describe("catalog API examples", () => {
  it("keeps the public request ID in every format without adding a resource pool prefix", () => {
    for (const protocol of catalogApiProtocols(model)) {
      for (const language of ["curl", "python", "javascript"] as const) {
        const code = catalogApiExample(model, "https://gateway.example/prefix", protocol, language);
        expect(code).toContain(JSON.stringify(model.id));
        expect(code).toContain("LITELLM_API_KEY");
        expect(code).toContain("https://gateway.example/prefix");
        expect(code).not.toContain("/v1/v1/");
      }
    }
  });

  it("uses a single version path and the proper Anthropic SDK base", () => {
    expect(catalogApiExample(model, "https://gateway.example/v1/", "anthropic", "curl")).toContain(
      "https://gateway.example/v1/messages",
    );
    expect(catalogApiExample(model, "https://gateway.example/v1/", "anthropic", "python")).toContain(
      'base_url="https://gateway.example"',
    );
  });

  it("uses a quoted heredoc for IDs containing shell characters", () => {
    const special = { ...model, id: "model'$(touch /tmp/unwanted)" };
    const code = catalogApiExample(special, "https://gateway.example", "openai", "curl");
    expect(code).toContain("--data @- <<'JSON'");
    expect(code).toContain(JSON.stringify(special.id));
  });

  it("exposes only examples for known supported modes and author formats", () => {
    expect(catalogApiProtocols({ ...model, mode: null })).toEqual([]);
    expect(catalogApiProtocols({ ...model, mode: "image_generation" })).toEqual([]);
    const embedding = { ...model, id: "embedding-fixture", provider: null, mode: "embedding" };
    expect(catalogApiProtocols(embedding)).toEqual(["openai"]);
    expect(catalogApiExample(embedding, "https://gateway.example", "openai", "python")).toContain(
      "client.embeddings.create",
    );
  });
});
