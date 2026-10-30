import { describe, expect, it } from "vitest";
import { recordedResourceRoute } from "./resourceRoute";

describe("recordedResourceRoute", () => {
  it("uses recorded pool provenance independently of the API integration", () => {
    expect(
      recordedResourceRoute({
        retail_resource_pool_id: "aws-pool",
        retail_resource_pool_name: "AWS Bedrock Claude",
        retail_resource_source_kind: "bedrock",
        custom_llm_provider: "anthropic",
      }),
    ).toEqual({ poolId: "aws-pool", poolName: "AWS Bedrock Claude", sourceKind: "bedrock" });
  });

  it("reads nested records but never infers a resource pool from provider or model", () => {
    expect(recordedResourceRoute({ litellm_metadata: { retail_resource_pool_id: "pool" } })).toEqual({
      poolId: "pool",
      poolName: null,
      sourceKind: null,
    });
    expect(recordedResourceRoute({ custom_llm_provider: "anthropic", model: "claude-test" })).toEqual({
      poolId: null,
      poolName: null,
      sourceKind: null,
    });
    expect(recordedResourceRoute(null)).toEqual({ poolId: null, poolName: null, sourceKind: null });
  });
});
