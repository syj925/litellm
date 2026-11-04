"use client";

import { useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { CalendarDays, Plus, RefreshCw } from "lucide-react";
import { useQueryStates } from "nuqs";
import { useCallback, useId, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/lib/toast";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { retailRequest, type PageResult } from "../api";
import { ManagementError, managementQuery } from "../management/ManagementShared";
import { BudgetPagination, useModelBudgetConfiguration } from "../model-budgets/BudgetShared";
import MonthlyBudgetForm from "./MonthlyBudgetForm";
import MonthlyBudgetNote from "./MonthlyBudgetNote";
import MonthlyBudgetsTable from "./MonthlyBudgetsTable";
import { monthlyBudgetParsers } from "./monthlyBudgetInput";
import type { AdminMonthlyBudget, MonthlyBudgetDraft } from "./types";

function MonthlyWorkspace({
  userId,
  actorId,
  accessToken,
  onSavingChange,
}: {
  userId: string;
  actorId: string;
  accessToken: string;
  onSavingChange: (saving: boolean) => void;
}) {
  const id = useId();
  const client = useQueryClient();
  const [filters, setFilters] = useQueryStates(monthlyBudgetParsers);
  const [draft, setDraft] = useState<MonthlyBudgetDraft | null>(null);
  const [authorityLost, setAuthorityLost] = useState(false);
  const parameters = { q: filters.monthly_q, limit: 25, offset: (filters.monthly_page - 1) * 25 };
  const options = {
    queryKey: ["retail", "monthly-budgets", actorId, "admin", userId, filters],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<AdminMonthlyBudget>>(
        managementQuery(`/admin/users/${encodeURIComponent(userId)}/monthly-budgets`, parameters),
        accessToken,
        { signal },
      ),
    retry: false,
    staleTime: 0,
    refetchOnMount: "always" as const,
    refetchInterval: (query: Query<PageResult<AdminMonthlyBudget>>): number | false =>
      query.state.error ? false : 30_000,
  };
  const plans = useQuery(options);
  const visible = !plans.error && !authorityLost && plans.isFetchedAfterMount ? plans.data : undefined;
  const unavailable = useCallback(() => {
    setAuthorityLost(true);
    setDraft(null);
    onSavingChange(false);
  }, [onSavingChange]);
  const refresh = async () => {
    setDraft(null);
    const result = await plans.refetch();
    if (!result.error) setAuthorityLost(false);
  };
  const saved = () => {
    setDraft(null);
    void client.invalidateQueries({ queryKey: ["retail", "monthly-budgets"] });
    void client.invalidateQueries({ queryKey: ["retail", "model-budgets"] });
    void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
    toast.success("Monthly plan saved");
  };
  return (
    <div className="space-y-5">
      <MonthlyBudgetNote />
      {plans.isPending && (
        <LoadingState title="Loading monthly plans" description="Retrieving calendar schedules and future limits." />
      )}
      {plans.error && <ManagementError title="Unable to load monthly plans" error={plans.error} />}
      {authorityLost && !plans.error && (
        <StateMessage
          role="alert"
          icon={<CalendarDays className="size-5" />}
          title="Monthly plan access changed"
          description="Reload to verify your current account access."
        />
      )}
      {(plans.error || authorityLost) && (
        <Button variant="outline" disabled={plans.isFetching} onClick={() => void refresh()}>
          Reload monthly plans
        </Button>
      )}
      {visible &&
        (draft ? (
          <MonthlyBudgetForm
            key={draft.kind === "create" ? "create" : `${draft.kind}:${draft.plan.id}:${draft.plan.version}`}
            userId={userId}
            draft={draft}
            onSaved={saved}
            onCancel={() => setDraft(null)}
            onReload={() => void refresh()}
            onUnavailable={unavailable}
            onSavingChange={onSavingChange}
          />
        ) : (
          <>
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="max-w-sm flex-1 space-y-2">
                <Label htmlFor={id}>Search monthly plan models</Label>
                <Input
                  id={id}
                  value={filters.monthly_q}
                  maxLength={100}
                  onChange={(event) => void setFilters({ monthly_q: event.target.value, monthly_page: 1 })}
                />
              </div>
              <div className="flex gap-2">
                <Button variant="outline" disabled={plans.isFetching} onClick={() => void refresh()}>
                  <RefreshCw />
                  Reload monthly plans
                </Button>
                <Button disabled={plans.isFetching} onClick={() => setDraft({ kind: "create" })}>
                  <Plus />
                  New monthly plan
                </Button>
              </div>
            </div>
            <div className="overflow-hidden rounded-xl border">
              {visible.items.length ? (
                <MonthlyBudgetsTable
                  items={visible.items}
                  disabled={plans.isFetching}
                  onEdit={(plan) => setDraft({ kind: "edit", plan })}
                  onStop={(plan) => setDraft({ kind: "stop", plan })}
                />
              ) : (
                <StateMessage
                  role="status"
                  icon={<CalendarDays className="size-5" />}
                  title="No matching monthly plans"
                  description="Create a recurring budget starting next full month or later, or try another model search."
                />
              )}
              <BudgetPagination
                page={filters.monthly_page}
                total={visible.total}
                loading={plans.isFetching}
                onPage={(page) => void setFilters({ monthly_page: page })}
              />
            </div>
          </>
        ))}
    </div>
  );
}

export default function AdminMonthlyBudgets({
  userId,
  onSavingChange,
}: {
  userId: string;
  onSavingChange: (saving: boolean) => void;
}) {
  const { accessToken, userId: actorId, userRole, isViewOnly, isAuthorized } = useAuthorized();
  const allowed = isAuthorized && canUseAdminControlPlane({ userRole, isViewOnly });
  const config = useModelBudgetConfiguration(allowed);
  if (!allowed || !accessToken || !actorId) return null;
  if (config.error) return <ManagementError title="Unable to verify monthly plan availability" error={config.error} />;
  if (config.isPending)
    return <LoadingState title="Loading monthly plan availability" description="Checking calendar budget support." />;
  if (config.data?.model_budgets_enabled !== true || config.data.monthly_budgets_enabled !== true) return null;
  return (
    <MonthlyWorkspace
      key={`${actorId}:${userId}`}
      userId={userId}
      actorId={actorId}
      accessToken={accessToken}
      onSavingChange={onSavingChange}
    />
  );
}
