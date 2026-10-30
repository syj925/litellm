export interface NamedChannel {
  id: string;
  name: string;
  pool_id: string;
  pool_name: string;
  deployment_ids: string[];
  models: string[];
  deployment_count: number;
  status: "active" | "provisioning" | "error";
  version: number;
  pending_deployment_ids?: string[];
}

export interface ChannelCatalog {
  enabled: boolean;
  items: NamedChannel[];
}

export type ChannelSelection =
  | { mode: "new"; name: string; pool_id: string }
  | { mode: "existing"; channel_id: string };

export interface ChannelConnectionSummary {
  channel_id: string;
  version: number;
  deployment_count: number;
  editable_deployment_count: number;
  can_edit: boolean;
  reason: string | null;
  api_base: string | null;
  api_base_mixed: boolean;
  has_api_key: boolean | null;
  credential_mode: "direct" | "referenced" | "mixed" | "unknown";
}

export interface ChannelConnectionPatch {
  api_base?: string;
  api_key?: string;
}
