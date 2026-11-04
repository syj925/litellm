import { z } from "zod";

function hasControlCharacters(value: string): boolean {
  return Array.from(value).some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127);
}

export const oidcStatusSchema = z
  .object({
    enabled: z.boolean(),
    available: z.boolean(),
    label: z
      .string()
      .max(80)
      .refine((value) => !hasControlCharacters(value)),
  })
  .strict();

export type OrganizationOidcStatus = z.infer<typeof oidcStatusSchema>;

export function supportsOrganizationOidc(
  config: { is_control_plane?: boolean } | undefined,
  worker: string | null,
): boolean {
  return config !== undefined && config.is_control_plane !== true && worker === null;
}

export function oidcGatewayBase(base: string, origin: string): string | null {
  if (/[\s\\]/.test(base)) return null;
  try {
    const parsed = new URL(base || "/", origin);
    if (parsed.origin !== origin) return null;
    if (parsed.username || parsed.password) return null;
    if (parsed.search || parsed.hash) return null;
    if (!/^https?:$/.test(parsed.protocol)) return null;
    return `${parsed.origin}${parsed.pathname.replace(/\/+$/, "")}`;
  } catch {
    return null;
  }
}

const credentialParameters =
  /^(?:token|jwt|key|api[_-]?key|x-api-key|password|secret|client_secret|authorization|access_token|id_token|refresh_token|code|state|return_to|redirect_to|oidc_error|worker)$/i;

function safeRawPath(value: string): boolean {
  const path = value.replace(/^https?:\/\/[^/?#]+/, "").split("?")[0];
  if (/%|\/\//.test(path)) return false;
  return !path.split("/").some((segment) => segment === "." || segment === "..");
}

export function oidcReturnPath(value: string | null, origin: string): string {
  if (!value || value.length > 2048) return "/ui/";
  if (hasControlCharacters(value) || /[\s\\#]/.test(value)) return "/ui/";
  if (!safeRawPath(value) || value.startsWith("//")) return "/ui/";
  if (!value.startsWith("/") && !/^https?:\/\//.test(value)) return "/ui/";
  try {
    const decoded = decodeURIComponent(value);
    if (hasControlCharacters(decoded) || decoded.includes("\\")) return "/ui/";
    const parsed = new URL(value, origin);
    if (parsed.origin !== origin || parsed.username || parsed.password) return "/ui/";
    if (parsed.pathname !== "/ui" && !parsed.pathname.startsWith("/ui/")) return "/ui/";
    if (/^\/ui\/(?:login|logout|callback)(?:\/|$)/i.test(parsed.pathname)) return "/ui/";
    if (Array.from(parsed.searchParams.keys()).some((key) => credentialParameters.test(key))) return "/ui/";
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return "/ui/";
  }
}

export function oidcErrorMessage(search: string): string | null {
  const code = new URLSearchParams(search).get("oidc_error");
  if (!code || !["failed", "denied", "expired", "unavailable"].includes(code)) return null;
  return "Organization sign-in could not be completed. Try again or use your username and password.";
}
