import type { Team } from "../key_team_helpers/key_list";
import {
  internalUserRoles,
  isOrgAdminSessionRole,
  isProxyAdminRole,
  isUserTeamAdminForAnyTeam,
  isUserTeamAdminForSingleTeam,
} from "@/utils/roles";
import { readResourcePolicy, type ResourceRoutingPolicy } from "./resourceRoutingPolicy";

export function isSelfServiceKeyContext(
  role: string | null,
  userId: string | null,
  teams: Team[] | null | undefined,
  teamId?: string | null,
): boolean {
  return (
    internalUserRoles.includes(role ?? "") &&
    !isUserTeamAdminForSingleTeam(
      teams?.find((team) => team.team_id === teamId)?.members_with_roles ?? null,
      userId ?? "",
    )
  );
}

export function canAssignKeyOwnership(role: string | null, userId: string | null, teams: Team[] | null): boolean {
  return isProxyAdminRole(role ?? "") || isOrgAdminSessionRole(role) || isUserTeamAdminForAnyTeam(teams, userId ?? "");
}

export const selfServiceKeyFields = [
  { name: "max_budget", label: "Max Budget (USD)", step: 0.01 },
  { name: "tpm_limit", label: "Tokens per minute (TPM)", step: 1 },
  { name: "rpm_limit", label: "Requests per minute (RPM)", step: 1 },
  { name: "max_parallel_requests", label: "Max Parallel Requests", step: 1 },
] as const;

const selfServiceFields = new Set<string>([
  "key_alias",
  "duration",
  "models",
  "max_budget",
  "budget_duration",
  "max_parallel_requests",
  "tpm_limit",
  "rpm_limit",
]);

export function selfServiceKeyValues(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).filter(([name]) => selfServiceFields.has(name)));
}

export function selfServiceKeyUpdate(
  values: Record<string, unknown>,
  dirtyFields: readonly string[],
): Record<string, unknown> {
  const changed = new Set(dirtyFields);
  let metadata = values.metadata;
  if (typeof metadata === "string") {
    try {
      metadata = JSON.parse(metadata);
    } catch {
      metadata = {};
    }
  }
  const policy: ResourceRoutingPolicy = readResourcePolicy(metadata) ?? { mode: "auto" };
  return {
    key: values.key ?? values.token,
    ...Object.fromEntries(
      Object.entries(selfServiceKeyValues(values)).filter(
        ([name, value]) => changed.has(name) && (name !== "duration" || value !== ""),
      ),
    ),
    ...(changed.has("metadata") ? { metadata: { retail_resource_policy: policy } } : {}),
  };
}
