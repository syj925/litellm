import { describe, expect, it } from "vitest";
import { RetailRequestError } from "../api";
import {
  canDiscardDeliveries,
  canEnableDelivery,
  canRetryDelivery,
  deliveryErrorText,
  deliveryParsers,
  deliveryRequest,
  deliveryRequestError,
  outstandingDeliveries,
} from "./deliveryState";
import type { AuditDeliveryJob, AuditDeliveryStatus } from "./types";

const status: AuditDeliveryStatus = {
  enabled: false,
  config_version: 3,
  configuration_matches: true,
  destination_configured: true,
  configuration_error: null,
  activated_at: null,
  retained_count: 2,
  overflow_count: 0,
  last_overflow_at: null,
  counts: { pending: 1, processing: 0, delivered: 0, failed: 1, discarded: 0 },
  last_delivered_at: null,
  last_failure_at: null,
  queue_capacity: 10000,
  max_attempts: 5,
  retention_days: 7,
  global_concurrency: 2,
};
const job: AuditDeliveryJob = {
  id: "delivery-fixture",
  event_id: "event-fixture",
  action: "model_budget.created",
  resource_type: "model_budget",
  occurred_at: "2026-11-04T00:00:00Z",
  state: "failed",
  attempts: 5,
  version: 7,
  created_at: "2026-11-04T00:00:00Z",
  completed_at: null,
  error_code: "timeout",
  next_attempt_at: null,
  configuration_matches: true,
};

describe("delivery permissions", () => {
  it("allows enabling a configured paused target", () => expect(canEnableDelivery(status)).toBe(true));
  it.each([
    { enabled: true },
    { destination_configured: false },
    { configuration_error: "invalid_configuration" as const },
    { configuration_matches: false },
  ])("does not enable an unavailable or mismatched target %o", (change) => {
    expect(canEnableDelivery({ ...status, ...change })).toBe(false);
  });
  it("allows a new configuration only after all outstanding states are cleared", () => {
    const counts = { pending: 0, processing: 0, delivered: 3, failed: 0, discarded: 2 };
    expect(canEnableDelivery({ ...status, configuration_matches: false, counts })).toBe(true);
  });
  it.each(["pending", "processing", "failed"] as const)(
    "counts %s as outstanding for configuration rotation",
    (state) => {
      const counts = { pending: 0, processing: 0, delivered: 0, failed: 0, discarded: 0, [state]: 1 };
      const changed = { ...status, counts, configuration_matches: false };
      expect(outstandingDeliveries(changed)).toBe(1);
      expect(canEnableDelivery(changed)).toBe(false);
    },
  );
  it("allows discard while paused even if the operator configuration disappeared", () => {
    expect(canDiscardDeliveries({ ...status, destination_configured: false })).toBe(true);
    expect(canDiscardDeliveries({ ...status, enabled: true })).toBe(false);
  });
  it("does not offer discard for completed history alone", () => {
    expect(
      canDiscardDeliveries({ ...status, counts: { pending: 0, processing: 0, delivered: 8, failed: 0, discarded: 3 } }),
    ).toBe(false);
  });
  it("retries a failed record only when the enabled receiver matches both scopes", () => {
    expect(canRetryDelivery(job, { ...status, enabled: true })).toBe(true);
    expect(canRetryDelivery(job, status)).toBe(false);
    expect(canRetryDelivery({ ...job, configuration_matches: false }, { ...status, enabled: true })).toBe(false);
    expect(canRetryDelivery(job, { ...status, enabled: true, configuration_matches: false })).toBe(false);
    expect(canRetryDelivery(job, { ...status, enabled: true, destination_configured: false })).toBe(false);
    expect(canRetryDelivery({ ...job, error_code: "expired" }, { ...status, enabled: true })).toBe(false);
  });
  it.each(["pending", "processing", "delivered", "discarded"] as const)("does not retry a %s record", (state) => {
    expect(canRetryDelivery({ ...job, state }, { ...status, enabled: true })).toBe(false);
  });
});

describe("delivery request boundaries", () => {
  it("keeps the configuration version in its write payload", () => {
    expect(deliveryRequest({ kind: "configure", enabled: true, expected_version: 7 })).toEqual({
      path: "/admin/audit-delivery",
      method: "PATCH",
      body: { enabled: true, expected_version: 7 },
    });
  });
  it("uses the selected delivery version and safely encodes its ID", () => {
    expect(deliveryRequest({ kind: "retry", id: "scope/id?x", expected_version: 4 })).toEqual({
      path: "/admin/audit-delivery/deliveries/scope%2Fid%3Fx/retry",
      method: "POST",
      body: { expected_version: 4 },
    });
  });
  it("requires explicit discard confirmation and a settings version", () => {
    expect(deliveryRequest({ kind: "discard", expected_version: 9, confirmation: "discard_pending" })).toEqual({
      path: "/admin/audit-delivery/discard",
      method: "POST",
      body: { expected_version: 9, confirmation: "discard_pending" },
    });
  });
  it.each(["0", "402", "1.5", "NaN"])("rejects an unbounded or invalid page %s", (input) => {
    expect(deliveryParsers.delivery_page.parse(input)).toBeNull();
  });
  it("keeps the highest page within the server offset limit", () => {
    expect(deliveryParsers.delivery_page.parse("401")).toBe(401);
    expect(deliveryParsers.delivery_state.parse("failed")).toBe("failed");
    expect(deliveryParsers.delivery_state.parse("unknown-private-input")).toBeNull();
  });
  it("does not echo an unknown receiver diagnostic", () => {
    const secret = "https://receiver.invalid/private?token=sensitive-marker";
    expect(deliveryErrorText(secret)).toBe("Delivery failed. Check the receiver before retrying.");
    expect(deliveryRequestError(new RetailRequestError(secret, 503))).not.toContain(secret);
  });
  it("provides a fixed recovery message for state conflicts", () => {
    expect(deliveryRequestError(new RetailRequestError("private details", 409))).toBe(
      "The delivery configuration or record changed. Reload and review before submitting again.",
    );
  });
});
