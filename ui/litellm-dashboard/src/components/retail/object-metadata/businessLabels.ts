import { createParser, parseAsStringLiteral } from "nuqs";
import { managementTextParser } from "../management/ManagementShared";
import { objectMetadataKinds } from "./types";

const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const businessLabelsParsers = {
  kind: parseAsStringLiteral(objectMetadataKinds).withDefault("key"),
  q: managementTextParser,
  page: createParser({
    parse: (value) => {
      const page = Number(value);
      return Number.isSafeInteger(page) && page >= 1 && page <= 401 ? page : null;
    },
    serialize: String,
  }).withDefault(1),
  target: createParser({ parse: (value) => (uuidPattern.test(value) ? value : null), serialize: String }).withDefault(
    "",
  ),
};

type LabelsResult = { ok: true; tags: string[] } | { ok: false; error: string };

export function parseBusinessLabels(value: string): LabelsResult {
  const tags = value.split(/\r?\n/).filter((tag) => tag !== "");
  if (tags.length > 10) return { ok: false, error: "Use no more than 10 labels." };
  if (new Set(tags).size !== tags.length) return { ok: false, error: "Each label must be unique." };
  for (const tag of tags) {
    if (tag.length > 40 || !/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(tag)) {
      return {
        ok: false,
        error:
          "Each label must start with a lowercase letter and use lowercase letters, numbers, and single hyphens, up to 40 characters. Spaces are not allowed.",
      };
    }
    if (uuidPattern.test(tag) || /^[a-f0-9]{16,}$/.test(tag) || /[0-9]{16,}/.test(tag)) {
      return { ok: false, error: "Use a business category instead of an identifier or credential." };
    }
  }
  return { ok: true, tags };
}
