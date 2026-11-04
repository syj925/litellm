import { createParser, parseAsStringLiteral } from "nuqs";
import { RetailRequestError } from "../api";
import { deliveryStates, type AuditDeliveryJob, type AuditDeliveryStatus, type DeliveryCommand } from "./types";

const deliveryPageParser = createParser({
  parse: (value) => {
    const page = Number(value);
    return Number.isSafeInteger(page) && page >= 1 && page <= 401 ? page : null;
  },
  serialize: String,
}).withDefault(1);

export const deliveryParsers = {
  delivery_state: parseAsStringLiteral(["", ...deliveryStates] as const).withDefault(""),
  delivery_page: deliveryPageParser,
};

export const deliveryStateLabels = {
  pending: "Queued",
  processing: "Sending",
  delivered: "Delivered",
  failed: "Failed",
  discarded: "Discarded",
} satisfies Record<(typeof deliveryStates)[number], string>;

const deliveryErrors: Record<string, string> = {
  timeout: "The receiver did not respond in time.",
  transport_error: "The receiver connection failed.",
  http_retryable: "The receiver was busy or temporarily unavailable.",
  http_rejected: "The receiver rejected the delivery.",
  redirect_rejected: "The receiver redirected the request. Redirects are not followed.",
  lease_expired: "The worker did not complete its delivery lease.",
  expired: "The delivery exceeded its retention window.",
  discarded: "An administrator discarded this delivery.",
};

export function deliveryErrorText(code: string | null): string {
  if (code === null) return "No failure recorded";
  return deliveryErrors[code] ?? "Delivery failed. Check the receiver before retrying.";
}

export function outstandingDeliveries(status: AuditDeliveryStatus): number {
  return status.counts.pending + status.counts.processing + status.counts.failed;
}

export function canEnableDelivery(status: AuditDeliveryStatus): boolean {
  const configured = status.destination_configured && status.configuration_error === null;
  const compatible = status.configuration_matches || outstandingDeliveries(status) === 0;
  return !status.enabled && configured && compatible;
}

export function canRetryDelivery(job: AuditDeliveryJob, status: AuditDeliveryStatus): boolean {
  const receiverReady = status.enabled && status.destination_configured && status.configuration_error === null;
  const matching = status.configuration_matches && job.configuration_matches;
  const retryable = job.state === "failed" && job.error_code !== "expired";
  return receiverReady && matching && retryable;
}

export function canDiscardDeliveries(status: AuditDeliveryStatus): boolean {
  return !status.enabled && outstandingDeliveries(status) > 0;
}

export function deliveryRequest(command: DeliveryCommand): { path: string; method: "POST" | "PATCH"; body: object } {
  const base = "/admin/audit-delivery";
  if (command.kind === "configure") {
    return {
      path: base,
      method: "PATCH",
      body: { enabled: command.enabled, expected_version: command.expected_version },
    };
  }
  if (command.kind === "retry") {
    return {
      path: `${base}/deliveries/${encodeURIComponent(command.id)}/retry`,
      method: "POST",
      body: { expected_version: command.expected_version },
    };
  }
  return {
    path: `${base}/discard`,
    method: "POST",
    body: { expected_version: command.expected_version, confirmation: command.confirmation },
  };
}

export function deliveryAccessLost(error: Error): boolean {
  return error instanceof RetailRequestError && [401, 403, 404].includes(error.status);
}

export function deliveryRequestError(error: Error): string {
  if (deliveryAccessLost(error)) return "Current administrator access is unavailable. Reload to check your access.";
  if (error instanceof RetailRequestError && error.status === 409) {
    return "The delivery configuration or record changed. Reload and review before submitting again.";
  }
  if (error instanceof RetailRequestError && error.status === 422) {
    return "The operation is not valid for the current delivery state. Reload and review the configuration.";
  }
  return "The delivery request could not be completed. You can retry the same operation or reload its current state.";
}
