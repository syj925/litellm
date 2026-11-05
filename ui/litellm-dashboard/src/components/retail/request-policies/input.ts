import type { PolicyDefinition, PolicyRoute } from "./types";

export const policyRoutes: readonly PolicyRoute[] = ["chat.completions", "responses", "messages"];
export interface DefinitionFields {
  output: string;
  temperature: string;
  topP: string;
  seed: string;
}
export function definitionFields(definition?: PolicyDefinition): DefinitionFields {
  return {
    output: String(definition?.max_output_tokens ?? ""),
    temperature: String(definition?.temperature ?? ""),
    topP: String(definition?.top_p ?? ""),
    seed: String(definition?.seed ?? ""),
  };
}
export function buildDefinition(fields: DefinitionFields, routes: PolicyRoute[] | null): PolicyDefinition {
  const number = (raw: string, label: string, bounds: readonly [number, number], integer = false): number | null => {
    const [minimum, maximum] = bounds;
    if (!raw.trim()) return null;
    const value = Number(raw);
    const outsideRange = !Number.isFinite(value) || value < minimum || value > maximum;
    const invalidInteger = integer && !Number.isSafeInteger(value);
    if (outsideRange || invalidInteger) {
      throw new Error(`${label} must be ${integer ? "an integer" : "a number"} from ${minimum} to ${maximum}.`);
    }
    return value;
  };
  return {
    allowed_routes: routes,
    max_output_tokens: number(fields.output, "Output cap", [1, 1_000_000], true),
    temperature: number(fields.temperature, "Temperature", [0, 2]),
    top_p: number(fields.topP, "Top P", [0, 1]),
    seed: number(fields.seed, "Seed", [Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER], true),
  };
}
