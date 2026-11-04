"use client";

import { useQuery } from "@tanstack/react-query";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { retailRequest, type PageResult } from "../../api";
import { managementPageSize, managementQuery } from "../../management/ManagementShared";
import { useOrganizationFinanceConfiguration } from "../FinanceShared";
import { delegatedActor, delegatedError } from "./input";
import type { FinanceScope } from "./types";
import type { RetailOrganization } from "../../organizations/types";

export function useDelegationConfiguration(enabled = true) {
  const { isAuthorized, userRole, isViewOnly } = useAuthorized();
  const actor = enabled && isAuthorized && delegatedActor(userRole, isViewOnly);
  const config = useOrganizationFinanceConfiguration(actor);
  const flags = config.data as typeof config.data & { org_finance_delegation_enabled?: boolean };
  const financeEnabled = actor && !config.error && flags?.org_finance_enabled === true;
  return {
    config,
    enabled: financeEnabled && flags.org_finance_delegation_enabled === true,
  };
}

export function useFinanceScopes(enabled: boolean, q: string, page: number, ids?: string[]) {
  const { accessToken, userId } = useAuthorized();
  const parameters = { q, limit: managementPageSize, offset: (page - 1) * managementPageSize, ids: ids?.join(",") };
  const options = {
    queryKey: ["retail", "delegated-finance-scopes", userId, q, page, ids ?? null],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<FinanceScope>>(
        managementQuery("/organization-finance/organizations", parameters),
        accessToken,
        { signal },
      ),
    enabled: enabled && Boolean(accessToken),
    retry: false,
    staleTime: 0,
    refetchOnMount: "always" as const,
  };
  return useQuery(options);
}

export function useDirectoryFinance(items: RetailOrganization[] | undefined, error: Error | null, admin: boolean) {
  const delegation = useDelegationConfiguration(!admin);
  const available = Boolean(items) && !error;
  const enabled = !admin && delegation.enabled && available;
  const scopes = useFinanceScopes(
    enabled,
    "",
    1,
    items?.map((item) => item.id),
  );
  const verified = enabled && scopes.isFetchedAfterMount && !scopes.error;
  const scopeItems = verified ? scopes.data?.items ?? [] : [];
  return {
    ids: new Set(scopeItems.filter((item) => item.can_view && item.access_revision).map((item) => item.id)),
    error: enabled ? scopes.error : null,
    refresh: () => {
      if (enabled) void scopes.refetch();
    },
  };
}

export function DirectoryFinanceNotice({ error }: { error: Error | null }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-sm text-destructive">
      Finance access could not be verified. Refresh organizations to check again.
    </p>
  );
}

export function DelegatedError({ error }: { error: Error }) {
  return (
    <p role="alert" className="text-sm text-destructive">
      {delegatedError(error)}
    </p>
  );
}
