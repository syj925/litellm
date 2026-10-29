import { describe, expect, it } from "vitest";
import { KeyResourcePool, readResourcePolicy, resourcePolicyError, withResourcePolicy } from "./resourceRoutingPolicy";

const pools: KeyResourcePool[] = [
  { id: "pool-a", name: "Pool A", enabled: true, models: ["alpha", "beta"], allowed_models: ["alpha"] },
  { id: "pool-b", name: "Pool B", enabled: true, models: ["alpha", "beta"], allowed_models: ["beta"] },
  { id: "disabled", name: "Disabled", enabled: false, models: ["alpha"], allowed_models: ["alpha"] },
];

describe("key resource routing", () => {
  it("retains exact pool-model pairs instead of expanding their Cartesian product", () => {
    const policy = {
      mode: "custom" as const,
      grants: [
        { pool_id: "pool-a", models: ["alpha"] },
        { pool_id: "pool-b", models: ["beta"] },
      ],
    };
    expect(resourcePolicyError(policy, pools)).toBeNull();
    expect(withResourcePolicy('{"application":"example"}', policy)).toEqual({
      application: "example",
      retail_resource_policy: policy,
    });
    expect(resourcePolicyError({ mode: "custom", grants: [{ pool_id: "pool-a", models: ["beta"] }] }, pools)).toContain(
      "only models authorized",
    );
  });

  it("rejects empty custom policies, missing pools, and disabled pools", () => {
    expect(resourcePolicyError({ mode: "custom", grants: [] }, pools)).not.toBeNull();
    expect(resourcePolicyError({ mode: "custom", grants: [{ pool_id: "pool-a", models: [] }] }, pools)).not.toBeNull();
    expect(resourcePolicyError({ mode: "fixed", pool_id: "missing" }, pools)).not.toBeNull();
    expect(resourcePolicyError({ mode: "fixed", pool_id: "disabled" }, pools)).not.toBeNull();
  });

  it("overrides reserved metadata only from the structured selection and preserves other metadata", () => {
    expect(
      withResourcePolicy(
        { application: "example", retail_resource_policy: { mode: "fixed", pool_id: "other" } },
        { mode: "auto" },
      ),
    ).toEqual({ application: "example", retail_resource_policy: { mode: "auto" } });
    expect(withResourcePolicy({ application: "example", retail_resource_policy: { mode: "auto" } }, null)).toEqual({
      application: "example",
    });
  });

  it("reads saved custom pairs without adding any account-wide grant", () => {
    const policy = { mode: "custom", grants: [{ pool_id: "pool-a", models: ["alpha"] }] };
    expect(readResourcePolicy({ retail_resource_policy: policy })).toEqual(policy);
  });
});
