import { expect, it } from "vitest";
import { addDeploymentBindings, deploymentGroups, matchingDeploymentGroups } from "./deploymentGroups";
import type { PoolDeployment } from "./types";

const alpha: PoolDeployment = {
  deployment_id: "deployment-a",
  model: "shared-model",
  provider: "anthropic",
  credential_name: null,
  channel_id: "channel-a",
  channel_name: "Official production",
  pool_id: "pool-a",
  pool_name: "Source A",
};
const beta: PoolDeployment = { ...alpha, deployment_id: "deployment-b", model: "other-model" };
const otherChannel: PoolDeployment = {
  ...alpha,
  deployment_id: "deployment-c",
  channel_id: "channel-b",
  channel_name: "Cloud production",
  pool_id: "pool-b",
  pool_name: "Source B",
};

it("groups only explicit channel membership even when public model names and providers match", () => {
  const legacyA = { ...alpha, deployment_id: "legacy-a", channel_id: null, channel_name: null };
  const legacyB = { ...legacyA, deployment_id: "legacy-b" };
  const groups = deploymentGroups([alpha, beta, otherChannel, legacyA, legacyB]);
  expect(
    groups.map((group) => ({
      name: group.name,
      pool: group.poolId,
      ids: group.items.map((item) => item.deployment_id),
    })),
  ).toEqual([
    { name: "Official production", pool: "pool-a", ids: ["deployment-a", "deployment-b"] },
    { name: "Cloud production", pool: "pool-b", ids: ["deployment-c"] },
    { name: "Unnamed deployment", pool: "pool-a", ids: ["legacy-a"] },
    { name: "Unnamed deployment", pool: "pool-a", ids: ["legacy-b"] },
  ]);
});

it("keeps all channel models visible when search matches one member", () => {
  const groups = deploymentGroups([alpha, beta, otherChannel]);
  expect(matchingDeploymentGroups(groups, "other-model").map((group) => group.items)).toEqual([[alpha, beta]]);
  expect(matchingDeploymentGroups(groups, "cloud production").map((group) => group.items)).toEqual([[otherChannel]]);
});

it("adds a whole channel without duplicating existing members or resetting their weights", () => {
  const bindings = [{ deployment_id: alpha.deployment_id, model: alpha.model, weight: "7", priority: "3" }];
  expect(addDeploymentBindings(bindings, [alpha, beta])).toEqual([
    bindings[0],
    { deployment_id: beta.deployment_id, model: beta.model, weight: "1", priority: "0" },
  ]);
  expect(bindings).toEqual([{ deployment_id: alpha.deployment_id, model: alpha.model, weight: "7", priority: "3" }]);
});
