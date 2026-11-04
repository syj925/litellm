export const deliveryStates = ["pending", "processing", "delivered", "failed", "discarded"] as const;
export type DeliveryState = (typeof deliveryStates)[number];

export interface AuditDeliveryStatus {
  enabled: boolean;
  config_version: number;
  configuration_matches: boolean;
  destination_configured: boolean;
  configuration_error: "not_configured" | "invalid_configuration" | null;
  activated_at: string | null;
  retained_count: number;
  overflow_count: number;
  last_overflow_at: string | null;
  counts: Record<DeliveryState, number>;
  last_delivered_at: string | null;
  last_failure_at: string | null;
  queue_capacity: number;
  max_attempts: number;
  retention_days: number;
  global_concurrency: number;
}

export interface AuditDeliveryJob {
  id: string;
  event_id: string;
  action: string;
  resource_type: string;
  occurred_at: string;
  state: DeliveryState;
  attempts: number;
  version: number;
  created_at: string;
  completed_at: string | null;
  error_code: string | null;
  next_attempt_at: string | null;
  configuration_matches: boolean;
}

export type DeliveryCommand =
  | { kind: "configure"; enabled: boolean; expected_version: number }
  | { kind: "retry"; id: string; expected_version: number }
  | { kind: "discard"; expected_version: number; confirmation: "discard_pending" };
