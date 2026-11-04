import { expect, it } from "vitest";
import {
  oidcErrorMessage,
  oidcGatewayBase,
  oidcReturnPath,
  oidcStatusSchema,
  supportsOrganizationOidc,
} from "./oidcLoginInput";

const origin = "https://gateway.example";

it("limits independent OIDC to a discovered local gateway without a selected worker", () => {
  expect(supportsOrganizationOidc({}, null)).toBe(true);
  expect(supportsOrganizationOidc(undefined, null)).toBe(false);
  expect(supportsOrganizationOidc({ is_control_plane: true }, null)).toBe(false);
  expect(supportsOrganizationOidc({}, "worker-a")).toBe(false);
});

it.each(["/ui", "/ui/models/?search=public%20model&page=2", `${origin}/ui/logs/?page=2`])(
  "normalizes a same-gateway UI return target: %s",
  (target) => {
    const expected = new URL(target, origin);
    expect(oidcReturnPath(target, origin)).toBe(`${expected.pathname}${expected.search}`);
  },
);

it.each([
  null,
  "",
  "https://other.example/ui/models",
  "http://gateway.example/ui",
  "//other.example/ui",
  "/\\other.example/ui",
  "/ui/../outside",
  "/ui/./models",
  "/ui/%2e%2e/outside",
  "/ui/%252f/other",
  "/ui%2fmodels",
  "/ui//models",
  "/ui/%GG",
  "/outside",
  "/uievil/models",
  "/ui/login/",
  "/ui/login",
  "/ui/logout/",
  "/ui/callback/",
  `${origin}/ui/../outside`,
  `https://user:password@gateway.example/ui`,
  "/ui/models#fragment",
  "/ui?token=secret",
  "/ui?api_key=secret",
  "/ui?x-api-key=secret",
  "/ui?password=secret",
  "/ui?access_token=secret",
  "/ui?code=secret",
  "/ui?redirect_to=/ui/models",
  "/ui?worker=other",
  "/ui?%74oken=secret",
  "/ui?search=%0aheader",
  "/ui?search=%5cother",
  "/ui\t/models",
  "/ui\u007f/models",
  "/ui models",
  "javascript:alert(1)",
  "/ui?search=%GG",
  "/ui?search=" + "x".repeat(2048),
])("falls back safely for an unsupported return target: %s", (target) => {
  expect(oidcReturnPath(target, origin)).toBe("/ui/");
});

it.each(["", "/gateway", origin, `${origin}/gateway/`])("uses only a same-origin API base: %s", (base) => {
  const resolved = oidcGatewayBase(base, origin);
  expect(resolved).not.toBeNull();
  expect(new URL(resolved!).origin).toBe(origin);
});

it.each([
  "https://other.example",
  "//other.example",
  "http://gateway.example",
  `${origin}?token=x`,
  `${origin}#x`,
  "https://user:secret@gateway.example",
  "javascript:alert(1)",
  "/\\other.example",
  "/gateway path",
])("rejects a cross-worker or malformed API base: %s", (base) => {
  expect(oidcGatewayBase(base, origin)).toBeNull();
});

it("validates the exact public status projection without accepting identity metadata", () => {
  const status = { enabled: true, available: true, label: "Organization account" };
  expect(oidcStatusSchema.parse(status)).toEqual(status);
  expect(oidcStatusSchema.safeParse({ ...status, issuer: "private-issuer" }).success).toBe(false);
  expect(oidcStatusSchema.safeParse({ ...status, available: "true" }).success).toBe(false);
  expect(oidcStatusSchema.safeParse({ ...status, label: "unsafe\nlabel" }).success).toBe(false);
});

it.each(["failed", "denied", "expired", "unavailable"])(
  "maps only fixed OIDC error %s to safe recovery text",
  (code) => {
    const message = oidcErrorMessage(`?oidc_error=${code}&error_description=private-provider-secret`);
    expect(message).toContain("use your username and password");
    expect(message).not.toContain("private-provider-secret");
  },
);

it.each(["", "?oidc_error=private-provider-secret", "?error_description=private-provider-secret"])(
  "does not render arbitrary query errors: %s",
  (search) => {
    expect(oidcErrorMessage(search)).toBeNull();
  },
);
