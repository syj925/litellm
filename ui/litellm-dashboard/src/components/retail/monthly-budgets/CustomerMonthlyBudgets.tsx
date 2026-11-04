"use client";

import { useQuery, type Query } from "@tanstack/react-query";
import { CalendarDays, RefreshCw } from "lucide-react";
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
import { BudgetPagination, useModelBudgetConfiguration } from "../model-budgets/BudgetShared";
import MonthlyBudgetNote from "./MonthlyBudgetNote";
import MonthlyBudgetsTable from "./MonthlyBudgetsTable";
import { monthlyBudgetParsers } from "./monthlyBudgetInput";
import type { MonthlyBudget } from "./types";

function OwnMonthlyPlans({ userId, accessToken }: { userId: string; accessToken: string }) {
  const id = useId();
  const [filters, setFilters] = useQueryStates(monthlyBudgetParsers);
  const parameters = { q: filters.monthly_q, limit: 25, offset: (filters.monthly_page - 1) * 25 };
  const options = {
    queryKey: ["retail", "monthly-budgets", userId, "self", filters],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<MonthlyBudget>>(managementQuery("/monthly-budgets", parameters), accessToken, {
        signal,
      }),
    retry: false,
    staleTime: 0,
    refetchOnMount: "always" as const,
    refetchInterval: (query: Query<PageResult<MonthlyBudget>>): number | false => (query.state.error ? false : 30_000),
  };
  const plans = useQuery(options);
  const visible = !plans.error && plans.isFetchedAfterMount ? plans.data : undefined;
  return (
    <section className="overflow-hidden rounded-xl border" aria-label="Your monthly model budgets">
      <div className="space-y-4 border-b p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-medium">Your monthly model budgets</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Read-only calendar plans. The amount shown applies from its stated month; current usage is in your budget
              windows.
            </p>
          </div>
          <Button variant="outline" disabled={plans.isFetching} onClick={() => void plans.refetch()}>
            <RefreshCw />
            Refresh monthly plans
          </Button>
        </div>
        <MonthlyBudgetNote />
        <div className="max-w-sm space-y-2">
          <Label htmlFor={id}>Search your monthly plan models</Label>
          <Input
            id={id}
            maxLength={100}
            value={filters.monthly_q}
            onChange={(event) => void setFilters({ monthly_q: event.target.value, monthly_page: 1 })}
          />
        </div>
      </div>
      {plans.isPending && (
        <LoadingState title="Loading your monthly plans" description="Retrieving your own calendar budgets." />
      )}
      {plans.error && <ManagementError title="Unable to load your monthly plans" error={plans.error} />}
      {visible && (
        <>
          {visible.items.length ? (
            <MonthlyBudgetsTable items={visible.items} />
          ) : (
            <StateMessage
              role="status"
              icon={<CalendarDays className="size-5" />}
              title="No matching monthly plans"
              description="No recurring plan matches this view. Wallet funds and other access controls still apply."
            />
          )}
          <BudgetPagination
            page={filters.monthly_page}
            total={visible.total}
            loading={plans.isFetching}
            onPage={(page) => void setFilters({ monthly_page: page })}
          />
        </>
      )}
    </section>
  );
}

export default function CustomerMonthlyBudgets() {
  const { accessToken, userId, isAuthorized } = useAuthorized();
  const config = useModelBudgetConfiguration(isAuthorized);
  if (!isAuthorized || !accessToken || !userId) return null;
  if (config.error) return <ManagementError title="Unable to verify monthly plan availability" error={config.error} />;
  if (config.isPending)
    return <LoadingState title="Loading monthly plan availability" description="Checking calendar budget support." />;
  if (config.data?.model_budgets_enabled !== true || config.data.monthly_budgets_enabled !== true) return null;
  return <OwnMonthlyPlans key={userId} userId={userId} accessToken={accessToken} />;
}
