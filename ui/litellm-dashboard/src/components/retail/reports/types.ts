export type RetailReportState = "queued" | "processing" | "ready" | "failed" | "revoked" | "expired";

export interface RetailReport {
  id: string;
  account_id: string;
  scope: "account";
  state: RetailReportState;
  start_at: string;
  end_at: string;
  snapshot_at: string;
  created_at: string;
  expires_at: string;
  partial: boolean;
  currency: string;
  timezone: string;
  total_rows: number;
  totals: {
    credit_nanos: string;
    charge_nanos: string;
    release_nanos: string;
    credit: string;
    charge: string;
    release: string;
  };
  unresolved: { pending: number; unknown: number; conflict: number };
  artifact_bytes?: number;
  error_code?: string | null;
}

export interface CreateRetailReport {
  start_at: string;
  end_at: string;
  kind?: "credit" | "charge" | "release";
  account_id?: string;
  currency: "USD";
  timezone: "Asia/Shanghai";
}
