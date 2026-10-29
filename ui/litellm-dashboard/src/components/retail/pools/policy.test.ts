import { expect, it } from "vitest";
import { updatePoolPair } from "./policy";
import { poolIdFromName } from "./types";

it("keeps allowed models paired with their selected pool instead of expanding a cross-product", () => {
  const grants = updatePoolPair(
    [{ pool_id: "pool-a", models: ["alpha"] }],
    { id: "pool-b", models: ["alpha", "beta"] },
    "beta",
    true,
  );
  expect(grants).toEqual([
    { pool_id: "pool-a", models: ["alpha"] },
    { pool_id: "pool-b", models: ["beta"] },
  ]);
});

it("narrows a wildcard to remaining current models when one model is explicitly removed", () => {
  expect(
    updatePoolPair(
      [
        { pool_id: "pool-a", models: "*" },
        { pool_id: "pool-b", models: ["alpha"] },
      ],
      { id: "pool-a", models: ["alpha", "beta"] },
      "alpha",
      false,
    ),
  ).toEqual([
    { pool_id: "pool-b", models: ["alpha"] },
    { pool_id: "pool-a", models: ["beta"] },
  ]);
});

it("removes an empty pool grant without affecting another pool", () => {
  expect(
    updatePoolPair(
      [
        { pool_id: "pool-a", models: ["alpha"] },
        { pool_id: "pool-b", models: ["beta"] },
      ],
      { id: "pool-a", models: ["alpha"] },
      "alpha",
      false,
    ),
  ).toEqual([{ pool_id: "pool-b", models: ["beta"] }]);
});

it("does not duplicate an existing pair or implicitly allow future models", () => {
  expect(
    updatePoolPair(
      [{ pool_id: "pool-a", models: ["alpha"] }],
      { id: "pool-a", models: ["alpha", "beta"] },
      "alpha",
      true,
    ),
  ).toEqual([{ pool_id: "pool-a", models: ["alpha"] }]);
});

it.each([
  ["AWS Bedrock Claude", "aws-bedrock-claude"],
  [" Anthropic Subscribe ", "anthropic-subscribe"],
  ["Model / source & name", "model-source-name"],
])("generates a stable identifier from %s", (name, expected) => {
  expect(poolIdFromName(name)).toBe(expected);
});
