import { describe, expect, it } from "vitest";
import type { Team } from "../key_team_helpers/key_list";
import {
  canAssignKeyOwnership,
  isSelfServiceKeyContext,
  selfServiceKeyUpdate,
  selfServiceKeyValues,
} from "./keyFormAccess";

const teams = [
  { team_id: "managed", members_with_roles: [{ user_id: "customer", role: "admin" }] },
  { team_id: "member", members_with_roles: [{ user_id: "customer", role: "user" }] },
] as Team[];

describe("customer key form access", () => {
  it("keeps personal and member keys self-service even for an admin of another team", () => {
    expect(canAssignKeyOwnership("Internal User", "customer", teams)).toBe(true);
    expect(isSelfServiceKeyContext("Internal User", "customer", teams)).toBe(true);
    expect(isSelfServiceKeyContext("internal_user", "customer", teams, "member")).toBe(true);
    expect(isSelfServiceKeyContext("Internal User", "customer", teams, "managed")).toBe(false);
    expect(isSelfServiceKeyContext("Admin", "customer", teams)).toBe(false);
    expect(canAssignKeyOwnership("Internal User", "customer", [])).toBe(false);
  });

  it("only accepts customer lifecycle and usage caps", () => {
    expect(
      selfServiceKeyValues({
        key_alias: "production",
        models: ["claude"],
        max_parallel_requests: 4,
        rpm_limit: 20,
        user_id: "another-user",
        team_id: "another-team",
        key_type: "default",
        allowed_routes: [],
        metadata: { disable_spend_logs: true },
        config: { api_key: "upstream-secret" },
        guardrails: [],
        disable_global_guardrails: true,
        object_permission: { mcp_servers: ["all"] },
      }),
    ).toEqual({ key_alias: "production", models: ["claude"], max_parallel_requests: 4, rpm_limit: 20 });
  });

  it("submits only changed limits and strips protected metadata from an explicit policy edit", () => {
    expect(
      selfServiceKeyUpdate(
        {
          token: "key-hash",
          key_alias: "renamed",
          max_budget: 100,
          rpm_limit: 50,
          allowed_routes: [],
          metadata: JSON.stringify({ retail_resource_policy: { mode: "fixed", pool_id: "aws" }, logging: [] }),
        },
        ["key_alias", "metadata", "allowed_routes"],
      ),
    ).toEqual({
      key: "key-hash",
      key_alias: "renamed",
      metadata: { retail_resource_policy: { mode: "fixed", pool_id: "aws" } },
    });
    expect(
      selfServiceKeyUpdate({ key: "key-hash", metadata: { logging: [] }, max_budget: 100, duration: "" }, [
        "rpm_limit",
        "duration",
      ]),
    ).toEqual({ key: "key-hash" });
  });
});
