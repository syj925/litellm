export type PolicyRoute = "chat.completions" | "responses" | "messages";
export type PolicyTab = "policies" | "prompts" | "bindings";
export type TargetKind = "user" | "key" | "model" | "team";
export const targetKindLabels: Readonly<Record<TargetKind, string>> = {
  user: "User",
  key: "Virtual key",
  model: "Public model",
  team: "Team",
};
export interface PolicyDefinition {
  allowed_routes: PolicyRoute[] | null;
  max_output_tokens: number | null;
  temperature: number | null;
  top_p: number | null;
  seed: number | null;
}
export interface VersionedResource {
  id: string;
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}
export interface SavedPrompt extends VersionedResource {
  name: string;
}
export interface PromptDetails extends SavedPrompt {
  content: string;
}
export interface RequestPolicy extends VersionedResource {
  name: string;
  definition: PolicyDefinition;
  prompt_id: string | null;
}
export interface PolicyBinding extends VersionedResource {
  target_id: string;
  policy_id: string;
  target_kind: TargetKind;
  target_label: string;
  policy_name: string;
}
export interface PolicyTarget {
  id: string;
  kind: TargetKind;
  label: string;
}
export type PolicyResource = SavedPrompt | RequestPolicy | PolicyBinding;
export interface PolicySession {
  token: string;
  onDenied: () => void;
}
