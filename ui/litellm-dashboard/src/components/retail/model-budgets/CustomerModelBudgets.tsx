"use client";

import { useQuery, type Query } from "@tanstack/react-query";
import { RefreshCw, Wallet } from "lucide-react";
import { useQueryStates } from "nuqs";
import { useId } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { retailRequest, type PageResult } from "../api";
import { ManagementError, managementQuery } from "../management/ManagementShared";
import CustomerMonthlyBudgets from "../monthly-budgets/CustomerMonthlyBudgets";
import { BudgetPagination, BudgetScopeNote, useModelBudgetConfiguration } from "./BudgetShared";
import { modelBudgetListParsers, modelBudgetPageSize } from "./modelBudgetInput";
import ModelBudgetsTable from "./ModelBudgetsTable";
import type { ModelBudget } from "./types";

function CustomerBudgetList() {
  const { accessToken, userId } = useAuthorized();
  const id = useId();
  const [filters, setFilters] = useQueryStates(modelBudgetListParsers);
  const parameters = {
    q: filters.budget_q,
    limit: modelBudgetPageSize,
    offset: (filters.budget_page - 1) * modelBudgetPageSize,
  };
  const options = {
    queryKey: ["retail", "model-budgets", userId, "self", filters],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<ModelBudget>>(managementQuery("/model-budgets", parameters), accessToken, { signal }),
    retry: false,
    refetchInterval: (query: Query<PageResult<ModelBudget>>): number | false => (query.state.error ? false : 30_000),
  };
  const budgets = useQuery(options);
  const visible = !budgets.error ? budgets.data : undefined;
  return (
    <section className="overflow-hidden rounded-xl border" aria-label="Your model budgets">
      <div className="space-y-4 border-b p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-medium">Your model budgets</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Fixed-window limits on your retail charges. Budget capacity is separate from your prepaid wallet balance.
            </p>
          </div>
          <Button variant="outline" disabled={budgets.isFetching} onClick={() => void budgets.refetch()}>
            <RefreshCw />
            Refresh model budgets
          </Button>
        </div>
        <BudgetScopeNote />
        <div className="max-w-sm space-y-2">
          <Label htmlFor={id}>Search your budget models</Label>
          <Input
            id={id}
            value={filters.budget_q}
            maxLength={100}
            onChange={(event) => void setFilters({ budget_q: event.target.value, budget_page: 1 })}
          />
        </div>
      </div>
      {budgets.isPending && (
        <LoadingState
          title="Loading your model budgets"
          description="Retrieving your own budget windows and retail charges."
        />
      )}
      {budgets.error && <ManagementError title="Unable to load your model budgets" error={budgets.error} />}
      {visible && (
        <>
          {visible.items.length ? (
            <ModelBudgetsTable items={visible.items} />
          ) : (
            <StateMessage
              role="status"
              icon={<Wallet className="size-5" />}
              title="No matching model budgets"
              description="No fixed-window model budget matches this view. Wallet funds and other access controls still apply."
            />
          )}
          <BudgetPagination
            page={filters.budget_page}
            total={visible.total}
            loading={budgets.isFetching}
            onPage={(page) => void setFilters({ budget_page: page })}
          />
        </>
      )}
    </section>
  );
}

export default function CustomerModelBudgets() {
  const { isAuthorized, accessToken } = useAuthorized();
  const config = useModelBudgetConfiguration(isAuthorized);
  if (!isAuthorized || !accessToken) return null;
  if (config.error) return <ManagementError title="Unable to verify model budget availability" error={config.error} />;
  if (config.isPending)
    return (
      <LoadingState
        title="Loading model budget availability"
        description="Checking which wallet features are enabled."
      />
    );
  if (config.data?.model_budgets_enabled !== true) return null;
  return (
    <div className="space-y-6">
      <CustomerBudgetList />
      {config.data.monthly_budgets_enabled === true && <CustomerMonthlyBudgets />}
    </div>
  );
}
