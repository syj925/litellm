import { describe, expect, it } from "vitest";
import { channelSelectionError, parseChannelSelection } from "./channelSelection";
import type { ResourcePool } from "../retail/pools/types";
import type { NamedChannel } from "../retail/channels/types";

const pool: ResourcePool = {
  id: "pool-a",
  name: "Source A",
  source_kind: "custom",
  description: "",
  enabled: false,
  visibility: "restricted",
  priority: 100,
  default_discount: "1",
  version: 1,
  bindings: [],
  models: [],
};
const channel: NamedChannel = {
  id: "channel-a",
  name: "Supplier A",
  pool_id: pool.id,
  pool_name: pool.name,
  deployment_ids: [],
  models: [],
  deployment_count: 0,
  status: "active",
  version: 1,
};

describe("channel selection", () => {
  it("preserves whitespace while typing and permits binding to a draft pool", () => {
    const value = { mode: "new", name: "  Supplier B  ", pool_id: pool.id };
    expect(parseChannelSelection(value)).toEqual(value);
    expect(channelSelectionError(value, [], [pool])).toBeNull();
  });
  it.each([null, {}, { mode: "new", name: "name" }, { mode: "existing", channel_id: null }])(
    "rejects an incomplete selection",
    (value) => {
      expect(parseChannelSelection(value)).toBeNull();
      expect(channelSelectionError(value, [channel], [pool])).not.toBeNull();
    },
  );
  it("requires an existing pool and nonblank channel name", () => {
    expect(channelSelectionError({ mode: "new", name: " ", pool_id: pool.id }, [], [pool])).toContain("channel name");
    expect(channelSelectionError({ mode: "new", name: "Supplier B", pool_id: "missing" }, [], [pool])).toContain(
      "resource pool",
    );
  });
  it("allows appending only to an active known channel with a valid pool", () => {
    const value = { mode: "existing", channel_id: channel.id };
    expect(channelSelectionError(value, [channel], [pool])).toBeNull();
    expect(channelSelectionError(value, [{ ...channel, status: "provisioning" }], [pool])).toContain(
      "available channel",
    );
    expect(channelSelectionError(value, [channel], [])).toContain("pool is unavailable");
  });
});
