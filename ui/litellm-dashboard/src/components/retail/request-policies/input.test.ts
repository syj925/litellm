import { expect, it } from "vitest";
import { buildDefinition } from "./input";

const empty = { output: "", temperature: "", topP: "", seed: "" };
it("represents blank limits as null while preserving explicit zero parameters and a deny-all route list", () => {
  const fields = { ...empty, temperature: "0", topP: "0", seed: "0" };
  const expected = {
    allowed_routes: [],
    max_output_tokens: null,
    temperature: 0,
    top_p: 0,
    seed: 0,
  };
  const unrestricted = {
    allowed_routes: null,
    max_output_tokens: null,
    temperature: null,
    top_p: null,
    seed: null,
  };
  expect(buildDefinition(fields, [])).toEqual(expected);
  expect(buildDefinition(empty, null)).toEqual(unrestricted);
});
it("preserves supported routes and the integer limits without seed precision loss", () => {
  const fields = { output: "1000000", temperature: "2", topP: "1", seed: "-9007199254740991" };
  const expected = {
    allowed_routes: ["messages"],
    max_output_tokens: 1000000,
    temperature: 2,
    top_p: 1,
    seed: -9007199254740991,
  };
  expect(buildDefinition(fields, ["messages"])).toEqual(expected);
});
it.each([
  ["output", "0"],
  ["output", "1.5"],
  ["output", "1000001"],
  ["temperature", "NaN"],
  ["temperature", "Infinity"],
  ["temperature", "2.01"],
  ["topP", "-0.1"],
  ["topP", "1.01"],
  ["seed", "9007199254740992"],
  ["seed", "-9007199254740992"],
  ["seed", "1.1"],
])("rejects invalid %s=%s before submitting", (key, value) =>
  expect(() => buildDefinition({ ...empty, [key]: value }, null)).toThrow(),
);
