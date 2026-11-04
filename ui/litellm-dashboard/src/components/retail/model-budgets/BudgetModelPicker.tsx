"use client";

import { useQuery } from "@tanstack/react-query";
import { useQueryStates } from "nuqs";
import { useEffect, useId } from "react";
import { LoadingState } from "@/components/shared/LoadingState";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RetailRequestError, retailRequest, type PageResult } from "../api";
import { ManagementError, managementQuery } from "../management/ManagementShared";
import { BudgetPagination } from "./BudgetShared";
import { modelBudgetModelParsers, modelBudgetPageSize } from "./modelBudgetInput";

export default function BudgetModelPicker({
  accessToken,
  actorId,
  userId,
  value,
  disabled,
  onSelect,
  onReady,
  onUnavailable,
}: {
  accessToken: string;
  actorId: string | null;
  userId: string;
  value: string;
  disabled: boolean;
  onSelect: (model: string) => void;
  onReady: (ready: boolean) => void;
  onUnavailable: () => void;
}) {
  const id = useId();
  const [filters, setFilters] = useQueryStates(modelBudgetModelParsers);
  const parameters = {
    q: filters.budget_model_q,
    limit: modelBudgetPageSize,
    offset: (filters.budget_model_page - 1) * modelBudgetPageSize,
  };
  const options = {
    queryKey: ["retail", "model-budgets", actorId, "models", userId, filters],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<{ model: string }>>(
        managementQuery(`/admin/users/${encodeURIComponent(userId)}/model-budgets/models`, parameters),
        accessToken,
        { signal },
      ),
    retry: false,
  };
  const models = useQuery(options);
  const visible = !models.error ? models.data : undefined;
  const ready = Boolean(visible?.items.some((item) => item.model === value));
  useEffect(() => onReady(ready), [ready, onReady]);
  useEffect(() => {
    const error = models.error;
    if (error instanceof RetailRequestError && [401, 403, 404].includes(error.status)) onUnavailable();
  }, [models.error, onUnavailable]);
  return (
    <section className="space-y-3" aria-label="Choose an exact public model">
      <div className="space-y-2">
        <Label htmlFor={`${id}-search`}>Search configured public models</Label>
        <Input
          id={`${id}-search`}
          value={filters.budget_model_q}
          maxLength={100}
          disabled={disabled}
          onChange={(event) => {
            onSelect("");
            void setFilters({ budget_model_q: event.target.value, budget_model_page: 1 });
          }}
        />
      </div>
      {models.isPending && (
        <LoadingState title="Loading public models" description="Checking configured exact model names." />
      )}
      {models.error && <ManagementError title="Unable to load public models" error={models.error} />}
      {visible && (
        <>
          <div className="space-y-2">
            <Label htmlFor={`${id}-model`}>Public model</Label>
            <select
              id={`${id}-model`}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={value}
              disabled={disabled || models.isFetching}
              onChange={(event) => onSelect(event.target.value)}
            >
              <option value="">Choose an exact public model</option>
              {visible.items.map((item) => (
                <option key={item.model} value={item.model}>
                  {item.model}
                </option>
              ))}
            </select>
            {visible.items.length === 0 && (
              <p role="status" className="text-sm text-muted-foreground">
                No configured public models match this search.
              </p>
            )}
          </div>
          <BudgetPagination
            page={filters.budget_model_page}
            total={visible.total}
            loading={models.isFetching || disabled}
            onPage={(page) => {
              onSelect("");
              void setFilters({ budget_model_page: page });
            }}
          />
        </>
      )}
    </section>
  );
}
