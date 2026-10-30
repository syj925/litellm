import { describe, expect, it } from "vitest";
import { RetailRequestError } from "../api";
import { channelConnectionPatch, channelConnectionSaveError } from "./channelConnection";

describe("channel connection payloads", () => {
  it("keeps existing URLs and keys unless a nonempty replacement is selected", () => {
    expect(channelConnectionPatch(false, "https://current.example/v1", "")).toBeNull();
    expect(channelConnectionPatch(true, " \t ", " \n ")).toBeNull();
    expect(channelConnectionPatch(false, "https://new.example/v1", " ")).toBeNull();
  });

  it("updates the exact URL without adding a path or submitting a blank key", () => {
    expect(channelConnectionPatch(true, " https://new.example/custom/v1 ", " \t ")).toEqual({
      api_base: "https://new.example/custom/v1",
    });
  });

  it("changes a key independently from mixed or unchanged URLs", () => {
    expect(channelConnectionPatch(false, "", " fixture-new-secret ")).toEqual({ api_key: "fixture-new-secret" });
  });

  it("does not reflect backend credential text into connection errors", () => {
    const secret = "fixture-sensitive-value";
    for (const status of [400, 403, 409, 500]) {
      expect(channelConnectionSaveError(new RetailRequestError(secret, status))).not.toContain(secret);
    }
    expect(channelConnectionSaveError(new Error(secret))).not.toContain(secret);
  });
});
