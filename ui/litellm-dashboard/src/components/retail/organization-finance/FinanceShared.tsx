"use client";

import { useQuery } from "@tanstack/react-query";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { retailRequest, type RetailConfig } from "../api";
import { financeError, type FinanceErrorContext } from "./financeInput";

export function useOrganizationFinanceConfiguration(enabled: boolean) {
  const { accessToken, userId, isAuthorized } = useAuthorized();
  const options = {
    queryKey: ["retail", "management-config", userId],
    queryFn: ({ signal }: { signal: AbortSignal }) => retailRequest<RetailConfig>("/config", accessToken, { signal }),
    enabled: enabled && isAuthorized && Boolean(accessToken),
    staleTime: 30_000,
    retry: false,
  };
  return useQuery(options);
}

export function organizationFinanceEnabled(
  config: RetailConfig | undefined,
  error: Error | null,
  canWrite: boolean,
): boolean {
  return canWrite && !error && config?.org_finance_enabled === true;
}

export function FinanceError({ error, context }: { error: Error; context: FinanceErrorContext }) {
  return (
    <p role="alert" className="text-sm text-destructive">
      {financeError(error, context)}
    </p>
  );
}
