export interface ModelBudget {
  id: string;
  model: string;
  effective_from: string;
  ends_at: string;
  limit_usd: string;
  spent_usd: string;
  held_usd: string;
  remaining_usd: string;
  overrun_usd: string;
  enabled: boolean;
  config_version: number;
  accounting_version: number;
  accounted_at: string | null;
  predecessor_id: string | null;
  successor_id: string | null;
  state: "scheduled" | "active" | "disabled" | "expired";
}

export interface AdminModelBudget extends ModelBudget {
  user_id: string;
  source: "management_api";
  created_at: string;
  created_by: string;
  updated_at: string;
  updated_by: string;
}

export interface CreateModelBudget {
  model: string;
  ends_at: string;
  limit_usd: string;
  enabled: boolean;
}

export interface UpdateModelBudget {
  limit_usd: string;
  enabled: boolean;
  expected_version: number;
}

export interface ScheduleModelBudget {
  ends_at: string;
  limit_usd: string;
  enabled: boolean;
  expected_version: number;
}

export type ModelBudgetDraft = { kind: "create" } | { kind: "edit" | "successor"; budget: AdminModelBudget };
