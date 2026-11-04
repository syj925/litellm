import { expect, it } from "vitest";
import { RetailRequestError } from "../../api";
import { delegatedActor, delegatedError, grantPayload, verifiedFinanceAccess } from "./input";

it("requires explicit valid grant choices and a nonnegative CAS version", () => {
  expect(grantPayload("none", 0)).toEqual({ can_view: false, can_export: false, expected_version: 0 });
  expect(grantPayload("view", 4)).toEqual({ can_view: true, can_export: false, expected_version: 4 });
  expect(grantPayload("export", 4)).toEqual({ can_view: true, can_export: true, expected_version: 4 });
  expect(grantPayload("other", 4)).toBeNull();
  expect(grantPayload("view", -1)).toBeNull();
});
it("does not mistake a native organization role for an effective explicit finance capability", () => {
  expect(delegatedActor("Internal User", false)).toBe(true);
  expect(delegatedActor("Org Admin", false)).toBe(true);
  expect(delegatedActor("Admin", true)).toBe(false);
  expect(delegatedActor("Unknown", false)).toBe(false);
  expect(verifiedFinanceAccess(undefined, "org-one")).toBe(false);
  const capability = {
    organization_id: "org-one",
    organization_name: "Organization",
    can_view: true,
    can_export: false,
    access_revision: "revision",
  };
  expect(verifiedFinanceAccess(capability, "org-two")).toBe(false);
  expect(verifiedFinanceAccess({ ...capability, can_view: false }, "org-one")).toBe(false);
  expect(verifiedFinanceAccess(capability, "org-one")).toBe(true);
});
it.each([401, 403, 404, 409, 410, 422, 500])("never renders server details for delegated error %s", (status) => {
  expect(delegatedError(new RetailRequestError("private-provider-marker", status))).not.toContain(
    "private-provider-marker",
  );
});
