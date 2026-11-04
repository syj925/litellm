"use client";

import { useQuery } from "@tanstack/react-query";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { retailRequest, type RetailConfig } from "../api";
import { ManagementPagination } from "../management/ManagementShared";
import { modelBudgetBrowseLimit } from "./modelBudgetInput";

export function useModelBudgetConfiguration(enabled: boolean) {
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

export function BudgetPagination({
  page,
  total,
  loading,
  onPage,
}: {
  page: number;
  total: number;
  loading: boolean;
  onPage: (page: number) => void;
}) {
  return (
    <>
      <ManagementPagination
        page={page}
        total={Math.min(total, modelBudgetBrowseLimit)}
        loading={loading}
        onChange={onPage}
      />
      {total > modelBudgetBrowseLimit && (
        <p role="status" className="border-t px-5 py-3 text-sm text-muted-foreground">
          There are {total.toLocaleString()} matches. Narrow your search to reach entries beyond the first 10,025.
        </p>
      )}
    </>
  );
}

export function BudgetScopeNote() {
  return (
    <div className="space-y-2 text-sm leading-relaxed text-muted-foreground">
      <p>
        Each exact public model shares one budget across this account’s API keys and resource pools. The window starts
        when the server creates it. Only successful reservations made during the window count; earlier usage and
        requests already in progress are excluded.
      </p>
      <p>
        Final charges can exceed the estimate. Actual retail charges are recorded, and the remaining budget controls new
        requests. Pausing enforcement preserves usage totals and continues tracking. Requests already held can settle
        against their original window after it ends. Wallet funds and model access are still required.
      </p>
    </div>
  );
}
