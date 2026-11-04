"use client";

import { useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { Plus, RefreshCw, Wallet } from "lucide-react";
import { useQueryState, useQueryStates } from "nuqs";
import { useCallback, useId, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/lib/toast";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { retailRequest, type PageResult } from "../api";
import { ManagementError, ManagementFeatureGate, managementQuery } from "../management/ManagementShared";
import AdminMonthlyBudgets from "../monthly-budgets/AdminMonthlyBudgets";
import { budgetModeParser, monthlyBudgetParsers } from "../monthly-budgets/monthlyBudgetInput";
import { BudgetPagination, BudgetScopeNote, useModelBudgetConfiguration } from "./BudgetShared";
import ModelBudgetForm from "./ModelBudgetForm";
import { modelBudgetListParsers, modelBudgetModelParsers, modelBudgetPageSize } from "./modelBudgetInput";
import ModelBudgetsTable from "./ModelBudgetsTable";
import type { AdminModelBudget, ModelBudgetDraft } from "./types";

function AdminBudgetWorkspace({
  userId,
  accessToken,
  actorId,
  onSavingChange,
  onManageMonthly,
}: {
  userId: string;
  accessToken: string;
  actorId: string | null;
  onSavingChange: (saving: boolean) => void;
  onManageMonthly?: (model: string) => void;
}) {
  const id = useId();
  const client = useQueryClient();
  const [filters, setFilters] = useQueryStates(modelBudgetListParsers);
  const [draft, setDraft] = useState<ModelBudgetDraft | null>(null);
  const [authorityLost, setAuthorityLost] = useState(false);
  const parameters = {
    q: filters.budget_q,
    limit: modelBudgetPageSize,
    offset: (filters.budget_page - 1) * modelBudgetPageSize,
  };
  const options = {
    queryKey: ["retail", "model-budgets", actorId, "admin", userId, filters],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<AdminModelBudget>>(
        managementQuery(`/admin/users/${encodeURIComponent(userId)}/model-budgets`, parameters),
        accessToken,
        { signal },
      ),
    retry: false,
    refetchInterval: (query: Query<PageResult<AdminModelBudget>>): number | false =>
      query.state.error ? false : 30_000,
  };
  const budgets = useQuery(options);
  const visible = !budgets.error && !authorityLost ? budgets.data : undefined;
  const closeForm = () => {
    setDraft(null);
  };
  const unavailable = useCallback(() => {
    setAuthorityLost(true);
    setDraft(null);
    onSavingChange(false);
  }, [onSavingChange]);
  const refresh = async () => {
    closeForm();
    const result = await budgets.refetch();
    if (!result.error) setAuthorityLost(false);
  };
  const saved = () => {
    closeForm();
    void client.invalidateQueries({ queryKey: ["retail", "model-budgets"] });
    void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
    toast.success(draft?.kind === "successor" ? "Next budget window scheduled" : "Model budget saved");
  };
  return (
    <div className="space-y-5">
      <BudgetScopeNote />
      {budgets.isPending && (
        <LoadingState
          title="Loading model budgets"
          description="Retrieving fixed windows and their current retail charges."
        />
      )}
      {budgets.error && (
        <>
          <ManagementError title="Unable to load model budgets" error={budgets.error} />
          <Button variant="outline" onClick={() => void refresh()} disabled={budgets.isFetching}>
            Reload budgets
          </Button>
        </>
      )}
      {authorityLost && !budgets.error && (
        <>
          <StateMessage
            role="alert"
            icon={<Wallet className="size-5" />}
            title="Budget access changed"
            description="Current budget access could not be verified. Reload budgets to check your scope again."
          />
          <Button variant="outline" onClick={() => void refresh()} disabled={budgets.isFetching}>
            Reload budgets
          </Button>
        </>
      )}
      {visible &&
        (draft ? (
          <ModelBudgetForm
            key={draft.kind === "create" ? "create" : `${draft.kind}:${draft.budget.id}:${draft.budget.config_version}`}
            accessToken={accessToken}
            actorId={actorId}
            userId={userId}
            draft={draft}
            onSaved={saved}
            onCancel={closeForm}
            onReload={() => void refresh()}
            onUnavailable={unavailable}
            onSavingChange={onSavingChange}
          />
        ) : (
          <>
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="max-w-sm flex-1 space-y-2">
                <Label htmlFor={id}>Search budget models</Label>
                <Input
                  id={id}
                  value={filters.budget_q}
                  maxLength={100}
                  onChange={(event) => void setFilters({ budget_q: event.target.value, budget_page: 1 })}
                />
              </div>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => void refresh()} disabled={budgets.isFetching}>
                  <RefreshCw />
                  Reload budgets
                </Button>
                <Button onClick={() => setDraft({ kind: "create" })} disabled={budgets.isFetching}>
                  <Plus />
                  New model budget
                </Button>
              </div>
            </div>
            <div className="overflow-hidden rounded-xl border">
              {visible.items.length ? (
                <ModelBudgetsTable
                  items={visible.items}
                  onEdit={(budget) => setDraft({ kind: "edit", budget })}
                  onSchedule={(budget) => setDraft({ kind: "successor", budget })}
                  onManageMonthly={onManageMonthly ? (budget) => onManageMonthly(budget.model) : undefined}
                  disabled={budgets.isFetching}
                />
              ) : (
                <StateMessage
                  role="status"
                  icon={<Wallet className="size-5" />}
                  title="No matching model budgets"
                  description="Create a fixed window for an exact public model, or try another search. Wallet funds and other access controls still apply."
                />
              )}
              <BudgetPagination
                page={filters.budget_page}
                total={visible.total}
                loading={budgets.isFetching}
                onPage={(page) => void setFilters({ budget_page: page })}
              />
            </div>
          </>
        ))}
    </div>
  );
}

export default function AdminModelBudgetsDialog({ userId, onClose }: { userId: string; onClose: () => void }) {
  const { accessToken, userId: actorId, userRole, isViewOnly, isAuthorized } = useAuthorized();
  const [saving, setSaving] = useState(false);
  const allowed = isAuthorized && canUseAdminControlPlane({ userRole, isViewOnly });
  const config = useModelBudgetConfiguration(allowed);
  const [mode, setMode] = useQueryState("budget_mode", budgetModeParser);
  const [, setMonthly] = useQueryStates(monthlyBudgetParsers);
  const [, setFilters] = useQueryStates(modelBudgetListParsers);
  const [, setModels] = useQueryStates(modelBudgetModelParsers);
  if (!isAuthorized || !accessToken || !canUseAdminControlPlane({ userRole, isViewOnly })) return null;
  const close = () => {
    if (saving) return;
    void setFilters({ budget_q: null, budget_page: null });
    void setModels({ budget_model_q: null, budget_model_page: null });
    void setMonthly({ monthly_q: null, monthly_page: null });
    void setMode(null);
    onClose();
  };
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-6xl">
        <DialogHeader>
          <DialogTitle>Account model budgets</DialogTitle>
          <DialogDescription className="break-all">{userId}</DialogDescription>
        </DialogHeader>
        <ManagementFeatureGate feature="model_budgets_enabled" title="Model budgets">
          {config.data?.monthly_budgets_enabled === true ? (
            <Tabs
              value={mode}
              onValueChange={(value) => {
                if (!saving && (value === "fixed" || value === "monthly")) void setMode(value);
              }}
              className="gap-5"
            >
              <TabsList variant="line">
                <TabsTrigger value="fixed" disabled={saving}>
                  Budget windows
                </TabsTrigger>
                <TabsTrigger value="monthly" disabled={saving}>
                  Monthly plans
                </TabsTrigger>
              </TabsList>
              <TabsContent value="fixed">
                <AdminBudgetWorkspace
                  userId={userId}
                  accessToken={accessToken}
                  actorId={actorId}
                  onSavingChange={setSaving}
                  onManageMonthly={(model) => {
                    void setMonthly({ monthly_q: model.slice(0, 100), monthly_page: 1 });
                    void setMode("monthly");
                  }}
                />
              </TabsContent>
              <TabsContent value="monthly">
                <AdminMonthlyBudgets userId={userId} onSavingChange={setSaving} />
              </TabsContent>
            </Tabs>
          ) : (
            <AdminBudgetWorkspace
              userId={userId}
              accessToken={accessToken}
              actorId={actorId}
              onSavingChange={setSaving}
            />
          )}
        </ManagementFeatureGate>
      </DialogContent>
    </Dialog>
  );
}
