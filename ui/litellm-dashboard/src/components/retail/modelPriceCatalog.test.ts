import { describe, expect, it } from "vitest";
import {
  displayedModelPrice,
  filterModelPrices,
  pricingPage,
  type CatalogPrice,
  type ModelPriceCatalogEntry,
} from "./modelPriceCatalog";

const entries: ModelPriceCatalogEntry[] = [
  {
    model: "public-chat",
    providers: ["provider-a"],
    modes: ["chat"],
    configured: true,
    status: "default",
    price: null,
    candidates: [],
    reason: null,
  },
  {
    model: "public-image",
    providers: ["provider-b"],
    modes: ["image_generation"],
    configured: true,
    status: "unavailable",
    price: null,
    candidates: [],
    reason: "Non-token rates",
  },
  {
    model: "custom-chat",
    providers: ["provider-a", "provider-b"],
    modes: ["chat"],
    configured: false,
    status: "custom",
    price: null,
    candidates: [],
    reason: null,
  },
];
const noFilters = { search: "", provider: "", mode: "", status: "" };
const referencePrice: CatalogPrice = {
  model: "public-chat",
  input_per_million: "2",
  output_per_million: "8",
  cache_read_per_million: "0.2",
  cache_write_per_million: "2",
  cache_write_price_published: false,
  catalog_model: "provider-a/context-tiered-model",
  source_label: "Test catalog",
  source: "catalog",
  version: null,
  updated_at: null,
};
const reviewEntry: ModelPriceCatalogEntry = {
  ...entries[0],
  status: "unavailable",
  review_references: [
    {
      catalog_model: referencePrice.catalog_model ?? "",
      source_label: "Test catalog",
      rates: { input_cost_per_token: "0.000002" },
      base_price: referencePrice,
    },
  ],
};

describe("model price catalog filtering", () => {
  it("combines search, provider, type and pricing status without omitting override-only entries", () => {
    const allFilters = { search: " CUSTOM   CHAT ", provider: "provider-b", mode: "chat", status: "custom" };
    expect(filterModelPrices(entries, allFilters)).toEqual([entries[2]]);
    expect(filterModelPrices(entries, { ...noFilters, provider: "provider-b", mode: "chat" })).toEqual([entries[2]]);
    expect(filterModelPrices(entries, { ...noFilters, status: "unavailable" })).toEqual([entries[1]]);
  });

  it("retains zero matches and resets to all rows with cleared filters", () => {
    expect(filterModelPrices(entries, { ...noFilters, search: "missing-model" })).toEqual([]);
    expect(filterModelPrices(entries, noFilters)).toEqual(entries);
  });

  it("clamps pagination after result changes instead of displaying an empty page", () => {
    const lastPage = { items: [entries[2]], index: 1, pageCount: 2, start: 2, size: 2 };
    const emptyPage = { items: [], index: 0, pageCount: 1, start: 0, size: 25 };
    expect(pricingPage(entries, 40, 2)).toEqual(lastPage);
    expect(pricingPage([], 8, 25)).toEqual(emptyPage);
    expect(pricingPage(entries, -1, 0).items).toEqual([entries[0]]);
  });

  it("previews only a single review reference and always gives saved effective prices precedence", () => {
    const multiple = {
      ...reviewEntry,
      review_references: [...(reviewEntry.review_references ?? []), ...(reviewEntry.review_references ?? [])],
    };
    const customPrice = { ...referencePrice, input_per_million: "3" };
    const saved = { ...reviewEntry, price: customPrice };
    const nonToken = {
      ...reviewEntry,
      review_references: [{ ...reviewEntry.review_references![0], base_price: null }],
    };
    expect(displayedModelPrice(reviewEntry)).toBe(referencePrice);
    expect(displayedModelPrice(multiple)).toBeNull();
    expect(displayedModelPrice(nonToken)).toBeNull();
    expect(displayedModelPrice(saved)).toBe(customPrice);
    expect(reviewEntry.price).toBeNull();
  });

  it("finds loaded internal reference names even when no effective flat price is available", () => {
    const filters = { ...noFilters, search: "context-tiered-model" };
    expect(filterModelPrices([reviewEntry], filters)).toEqual([reviewEntry]);
  });
});
