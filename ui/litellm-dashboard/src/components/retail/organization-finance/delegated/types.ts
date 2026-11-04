import type { OrganizationConsumptionSummary, ConsumptionState, OrganizationReportState } from "../types";

export type FinanceScope = {
  id: string;
  name: string;
  can_view: boolean;
  can_export: boolean;
  access_revision: string;
};
export type FinanceAccess = {
  organization_id: string;
  organization_name: string;
  can_view: boolean;
  can_export: boolean;
  access_revision: string | null;
};
export type DelegatedConsumption = {
  organization_id: string;
  organization_name: string;
  axis: "admission";
  access_revision: string;
  snapshot_at: string;
  start: string;
  end: string;
  partial: boolean;
  live: true;
  max_requests: number;
  total: number;
  limit: number;
  offset: number;
  summary: OrganizationConsumptionSummary;
  items: {
    sequence: number;
    model: string;
    captured_at: string;
    state: ConsumptionState;
    settled_usd: string | null;
  }[];
};
export type DelegatedReport = {
  id: string;
  scope: "delegated_organization";
  axis: "admission";
  organization_id: string;
  organization_name: string;
  access_revision: string;
  state: OrganizationReportState;
  start_at: string;
  end_at: string;
  snapshot_at: string;
  created_at: string;
  expires_at: string;
  partial: boolean;
  total_rows: number;
  summary: OrganizationConsumptionSummary;
  artifact_bytes: number;
};
export type FinanceGrant = {
  organization_id: string;
  user_id: string;
  grant_id: string | null;
  version: number;
  revision: string | null;
  can_view: boolean;
  can_export: boolean;
  effective: boolean;
  state:
    | "unassigned"
    | "revoked"
    | "active"
    | "organization_disabled"
    | "membership_inactive"
    | "membership_changed"
    | "organization_changed";
  updated_by: string | null;
  updated_at: string | null;
};
export type GrantUpdate = { can_view: boolean; can_export: boolean; expected_version: number };
export type DelegatedReportCreate = { start_at: string; end_at: string; currency: "USD"; timezone: "Asia/Shanghai" };
