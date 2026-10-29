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
