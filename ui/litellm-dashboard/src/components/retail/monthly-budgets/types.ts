export type MonthlyTimezone = "UTC" | "Asia/Shanghai";

export interface MonthlyBudget {
  id: string;
  model: string;
  currency: "USD";
  timezone: MonthlyTimezone;
  starts_at: string;
  stops_at: string | null;
  limit_usd: string;
  limit_effective_from: string;
  version: number;
  state: "scheduled" | "active" | "stopping" | "stopped";
  current_window_ready: boolean;
}

export interface AdminMonthlyBudget extends MonthlyBudget {
  user_id: string;
  created_at: string;
  created_by: string;
  updated_at: string;
  updated_by: string;
}

export interface CreateMonthlyBudget {
  model: string;
  start_month: string;
  timezone: MonthlyTimezone;
  limit_usd: string;
  currency: "USD";
}
export interface UpdateMonthlyBudget {
  limit_usd: string;
  expected_version: number;
}
export interface StopMonthlyBudget {
  expected_version: number;
}

export type MonthlyBudgetDraft = { kind: "create" } | { kind: "edit" | "stop"; plan: AdminMonthlyBudget };
