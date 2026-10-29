import { describe, expect, it } from "vitest";
import { groupConnections, toggleDeployments, type ChannelDeployment } from "./connections";

const deployment: ChannelDeployment = {
  deployment_id: "a",
  model: "same-model",
  provider: "anthropic",
  credential_name: null,
  endpoint_host: "api.example.test",
};

describe("existing upstream organization", () => {
  it("separates identical model names and endpoints when credential identities differ", () => {
    const grouped = groupConnections([
      { ...deployment, connection_id: "credential-a" },
      { ...deployment, deployment_id: "b", connection_id: "credential-b" },
      { ...deployment, deployment_id: "c", model: "other-model", connection_id: "credential-a" },
    ]);
    expect(grouped.map((group) => group.items.map((item) => item.deployment_id))).toEqual([["a", "c"], ["b"]]);
  });
  it("does not merge unidentified deployments by their model or provider", () => {
    expect(groupConnections([deployment, { ...deployment, deployment_id: "b" }])).toHaveLength(2);
  });
  it("selects a whole connection without dropping hidden existing selections", () => {
    expect(toggleDeployments(["hidden", "a"], ["a", "b"], true)).toEqual(["hidden", "a", "b"]);
    expect(toggleDeployments(["hidden", "a", "b"], ["a", "b"], false)).toEqual(["hidden"]);
  });
});
