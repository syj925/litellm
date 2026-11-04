export type BillingOrganization = {
  user_id: string;
  organization_id: string | null;
  organization_name: string | null;
  organization_enabled: boolean | null;
  membership_enabled: boolean | null;
  version: number;
  effective: boolean;
  state: "unassigned" | "active" | "organization_disabled" | "membership_inactive";
  updated_at: string | null;
  updated_by: string | null;
};

export type OrganizationChoice = { id: string; name: string; enabled?: boolean };
export type ConsumptionState = "settled" | "released" | "pending" | "unknown" | "conflict";
export type OrganizationConsumption = {
  organization_id: string;
  organization_name: string;
  organization_enabled: boolean;
  axis: "admission";
  snapshot_at: string;
  start: string;
  end: string;
  partial: boolean;
  live: true;
  max_requests: number;
  total: number;
  limit: number;
  offset: number;
  summary: {
    accepted_requests: number;
    settled_requests: number;
    released_requests: number;
    pending_requests: number;
    unknown_requests: number;
    conflict_requests: number;
    settled_usd: string;
    settled_nanos: string;
  };
  items: {
    request_id: string;
    user_id: string;
    model: string;
    captured_at: string;
    state: ConsumptionState;
    settled_usd: string | null;
    assignment_version: number;
    organization_version: number;
    membership_version: number;
  }[];
};
