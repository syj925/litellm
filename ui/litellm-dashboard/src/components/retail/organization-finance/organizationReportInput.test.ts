import { expect, it } from "vitest";
import { RetailRequestError } from "../api";
import {
  organizationReportError,
  organizationReportPayload,
  organizationReportPending,
  organizationReportRefreshNeeded,
} from "./organizationReportInput";
import type { OrganizationConsumption, OrganizationReportJob } from "./types";

it("creates an organization payload only from a successful matching enabled preview", () => {
  const preview = {
    organization_id: "org-one",
    organization_enabled: true,
    start: "2026-11-01T00:00:00Z",
    end: "2026-11-02T00:00:00Z",
  } as OrganizationConsumption;
  const expectedPayload = {
    start_at: preview.start,
    end_at: preview.end,
    currency: "USD",
    timezone: "Asia/Shanghai",
  };
  expect(organizationReportPayload(preview, "org-one")).toEqual(expectedPayload);
  expect(organizationReportPayload(preview, "org-two")).toBeNull();
  expect(organizationReportPayload({ ...preview, organization_enabled: false }, "org-one")).toBeNull();
  expect(organizationReportPayload(undefined, "org-one")).toBeNull();
});

it.each(["queued", "processing", "ready", "failed", "revoked", "expired"] as const)(
  "only polls active organization job state %s",
  (state) => {
    expect(organizationReportPending({ state } as OrganizationReportJob)).toBe(
      ["queued", "processing"].includes(state),
    );
  },
);

it("refreshes unavailable or changed artifact state and keeps authority failures separate", () => {
  expect(organizationReportRefreshNeeded(new RetailRequestError("private-path", 404))).toBe(true);
  expect(organizationReportRefreshNeeded(new RetailRequestError("private-path", 410))).toBe(true);
  expect(organizationReportRefreshNeeded(new RetailRequestError("private-path", 409))).toBe(true);
  expect(organizationReportRefreshNeeded(new RetailRequestError("private-path", 403))).toBe(false);
});

it.each([401, 403, 404, 409, 410, 422, 429, 500])("uses safe export recovery copy for HTTP %s", (status) => {
  const message = organizationReportError(new RetailRequestError("private-storage-path", status));
  expect(message).not.toContain("private-storage-path");
  expect(message.length).toBeGreaterThan(20);
});
