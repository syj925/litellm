import { describe, expect, it } from "vitest";
import { filterCatalog, type CatalogFilters } from "./catalogState";
import { formatCatalogPrice, modelPrice, type CatalogModel, type CatalogOffer } from "./types";

function model(id: string, values: Partial<CatalogModel> = {}): CatalogModel {
  return {
    id,
    name: id,
    provider: null,
    description: null,
    context_length: null,
    max_output_tokens: null,
    mode: "chat",
    input_modalities: ["text"],
    output_modalities: ["text"],
    metadata_source: null,
    available_to_me: false,
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
    offers: [],
    ...values,
  };
}

function offer(
  poolId: string,
  available: boolean,
  input: string | null,
  prices: { customer?: string | null; output?: string | null } = {},
): CatalogOffer {
  const customer = prices.customer ?? null;
  const output = prices.output === undefined ? input : prices.output;
  return {
    pool_id: poolId,
    pool_name: `Resource ${poolId}`,
    source_kind: "custom",
    available_to_me: available,
    pricing: {
      currency: "USD",
      unit: "million_tokens",
      reference: { input, output, cache_read: null, cache_write: null },
      customer: customer == null ? null : { input: customer, output: customer, cache_read: null, cache_write: null },
      discount: null,
      source_label: null,
      version: null,
      updated_at: null,
    },
  };
}

function filters(values: Partial<CatalogFilters> = {}): CatalogFilters {
  return {
    q: "",
    authors: [],
    pools: [],
    capabilities: [],
    input: [],
    context: 0,
    max_price: -1,
    availability: "all",
    mode: "all",
    sort: "name",
    price: "reference",
    ...values,
  };
}

const ids = (models: CatalogModel[]) => models.map(({ id }) => id);

describe("catalog filtering and price semantics", () => {
  it("includes public introductions independently of account access, with an explicit available-only filter", () => {
    const models = [model("alpha", { available_to_me: true }), model("beta")];
    expect(ids(filterCatalog(models, filters()))).toEqual(["alpha", "beta"]);
    expect(ids(filterCatalog(models, filters({ availability: "available" })))).toEqual(["alpha"]);
  });

  it("searches friendly model names and authors rather than requiring the exact request ID", () => {
    const models = [model("claude-opus-9-9-fixture"), model("gpt-9-fixture")];
    expect(ids(filterCatalog(models, filters({ q: "  claude opus 9.9  " })))).toEqual(["claude-opus-9-9-fixture"]);
    expect(ids(filterCatalog(models, filters({ q: "anthropic" })))).toEqual(["claude-opus-9-9-fixture"]);
    expect(ids(filterCatalog(models, filters({ authors: ["OpenAI"] })))).toEqual(["gpt-9-fixture"]);
  });

  it("combines resource pool, public model, modality and mode restrictions", () => {
    const models = [
      model("alpha-chat", {
        offers: [offer("A", true, "1"), offer("B", false, "2")],
        input_modalities: ["text", "image"],
      }),
      model("beta-chat", { offers: [offer("B", false, "2")], input_modalities: ["text", "image"] }),
      model("alpha-embedding", { offers: [offer("A", false, "1")], mode: "embedding" }),
    ];
    const selection = { pools: ["A"], q: "alpha", input: ["image"], mode: "chat" };
    expect(ids(filterCatalog(models, filters(selection)))).toEqual(["alpha-chat"]);
    expect(ids(filterCatalog(models, filters({ q: "Resource B", mode: "chat" })))).toEqual(["alpha-chat", "beta-chat"]);
  });

  it("matches only positively reported capabilities and leaves unknown models visible without capability filters", () => {
    const unknown = model("unknown");
    const unsupported = model("unsupported", { capabilities: { ...unknown.capabilities, tools: false } });
    const supported = model("supported", { capabilities: { ...unknown.capabilities, tools: true, vision: true } });
    const partial = model("partial", { capabilities: { ...unknown.capabilities, tools: true } });
    const models = [unknown, unsupported, supported, partial];
    expect(filterCatalog(models, filters())).toHaveLength(4);
    expect(ids(filterCatalog(models, filters({ capabilities: ["tools", "vision"] })))).toEqual(["supported"]);
  });

  it("sorts known context lengths descending, puts unknowns last and excludes unknowns from a minimum context filter", () => {
    const models = [model("unknown"), model("small", { context_length: 100 }), model("large", { context_length: 500 })];
    expect(ids(filterCatalog(models, filters({ sort: "context" })))).toEqual(["large", "small", "unknown"]);
    expect(ids(filterCatalog(models, filters({ context: 200 })))).toEqual(["large"]);
    expect(ids(models)).toEqual(["unknown", "small", "large"]);
  });

  it("sorts each price field independently, keeps missing prices last and distinguishes a listed zero from unknown", () => {
    const models = [
      model("unknown"),
      model("input-cheap", { offers: [offer("A", true, "1", { output: "5" })] }),
      model("output-cheap", { offers: [offer("B", true, "3", { output: "1" })] }),
      model("free", { offers: [offer("C", true, "0")] }),
    ];
    expect(ids(filterCatalog(models, filters({ sort: "input_price" })))).toEqual([
      "free",
      "input-cheap",
      "output-cheap",
      "unknown",
    ]);
    expect(ids(filterCatalog(models, filters({ sort: "output_price" })))).toEqual([
      "free",
      "output-cheap",
      "input-cheap",
      "unknown",
    ]);
    expect(ids(filterCatalog(models, filters({ max_price: 0 })))).toEqual(["free"]);
  });

  it("ignores unauthorized customer quotes even if incorrectly supplied, while preserving public reference prices", () => {
    const mixed = model("mixed", {
      offers: [offer("authorized", true, "10", { customer: "2" }), offer("public", false, "1", { customer: "0.001" })],
    });
    expect(modelPrice(mixed, "reference", "input")).toBe(1);
    expect(modelPrice(mixed, "customer", "input")).toBe(2);
    expect(
      modelPrice(
        model("unauthorized", { offers: [offer("public", false, "1", { customer: "0.001" })] }),
        "customer",
        "input",
      ),
    ).toBeNull();
  });

  it("does not render absent or invalid prices as free", () => {
    for (const value of [undefined, null, "", "   ", "NaN", "Infinity", "-1"]) {
      expect(formatCatalogPrice(value)).toBe("Not listed");
    }
    expect(formatCatalogPrice("0")).toBe("$0.00");
    expect(modelPrice(model("not-priced", { offers: [offer("A", true, null)] }), "reference", "input")).toBeNull();
  });
  it("excludes expression models from flat price comparisons even when stale flat rates are supplied", () => {
    const expression_policy = {
      version: "expression-v1" as const,
      expression: "p * 3",
      timezone: "UTC",
      usage_bounds: {},
    };
    const dynamic = model("dynamic", {
      expression_policy,
      offers: [offer("A", true, "0", { customer: "0" })],
      reference_pricing: { input: "0", output: "0", cache_read: null, cache_write: null },
    });
    expect(modelPrice(dynamic, "reference", "input")).toBeNull();
    expect(modelPrice(dynamic, "customer", "output")).toBeNull();
  });

  it("shows model reference rates independently of published resource availability without inventing a customer quote", () => {
    const introductoryModel = model("reference-only", {
      offers: [],
      reference_pricing: { input: "7", output: "14", cache_read: null, cache_write: null },
    });
    expect(modelPrice(introductoryModel, "reference", "input")).toBe(7);
    expect(modelPrice(introductoryModel, "customer", "input")).toBeNull();
  });
});
