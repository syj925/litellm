export const objectMetadataKinds = ["key", "team", "organization", "model"] as const;
export type ObjectMetadataKind = (typeof objectMetadataKinds)[number];

export interface BusinessLabels {
  tags: string[];
  version: number;
  source: "management_api" | null;
  created_at: string | null;
  created_by: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface MetadataTarget {
  id: string;
  kind: ObjectMetadataKind;
  name: string;
  object_created_at: string;
  object_created_by: string | null;
  object_updated_at: string | null;
  object_updated_by: string | null;
  metadata: BusinessLabels;
}

export const objectKindLabels: Record<ObjectMetadataKind, string> = {
  key: "API keys",
  team: "Teams",
  organization: "Organizations",
  model: "Models",
};
