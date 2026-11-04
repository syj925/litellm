"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatMoney } from "../api";
import { managementTimestamp } from "../management/ManagementShared";
import type { ModelBudget } from "./types";

function statusLabel(state: ModelBudget["state"]) {
  if (state === "disabled") return "Enforcement paused";
  if (state === "scheduled") return "Scheduled";
  return state;
}

export default function ModelBudgetsTable<T extends ModelBudget>({
  items,
  onEdit,
  onSchedule,
  onManageMonthly,
  disabled = false,
}: {
  items: T[];
  onEdit?: (budget: T) => void;
  onSchedule?: (budget: T) => void;
  onManageMonthly?: (budget: T) => void;
  disabled?: boolean;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-5">Public model</TableHead>
          <TableHead>Window (Beijing)</TableHead>
          <TableHead className="text-right">Limit (USD)</TableHead>
          <TableHead className="text-right">Settled (USD)</TableHead>
          <TableHead className="text-right">Held (USD)</TableHead>
          <TableHead className="text-right">Available (USD)</TableHead>
          <TableHead>Status</TableHead>
          {(onEdit || onSchedule) && (
            <TableHead className="pr-5">
              <span className="sr-only">Actions</span>
            </TableHead>
          )}
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((budget) => (
          <TableRow key={budget.id}>
            <TableCell className="max-w-56 break-all pl-5 font-medium">{budget.model}</TableCell>
            <TableCell className="min-w-44 text-xs">
              <div>{managementTimestamp(budget.effective_from)}</div>
              <div className="mt-1 text-muted-foreground">until {managementTimestamp(budget.ends_at)}</div>
            </TableCell>
            <TableCell className="text-right tabular-nums">{formatMoney(budget.limit_usd)}</TableCell>
            <TableCell className="text-right tabular-nums">{formatMoney(budget.spent_usd)}</TableCell>
            <TableCell className="text-right tabular-nums">{formatMoney(budget.held_usd)}</TableCell>
            <TableCell className="text-right tabular-nums">{formatMoney(budget.remaining_usd)}</TableCell>
            <TableCell>
              <div className="flex flex-col items-start gap-1">
                <Badge variant="outline">{statusLabel(budget.state)}</Badge>
                {budget.state === "scheduled" && !budget.enabled && (
                  <span className="text-xs text-muted-foreground">Enforcement paused</span>
                )}
                {budget.successor_id && <span className="text-xs text-muted-foreground">Next window scheduled</span>}
                {budget.recurrence_id && <span className="text-xs text-muted-foreground">Calendar-month plan</span>}
                {/[1-9]/.test(budget.overrun_usd) && (
                  <span className="text-xs text-destructive">Above limit: {formatMoney(budget.overrun_usd)}</span>
                )}
              </div>
            </TableCell>
            {(onEdit || onSchedule) && (
              <TableCell className="pr-5 text-right">
                <div className="flex flex-col items-end gap-2">
                  {budget.recurrence_id &&
                    (onManageMonthly ? (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={disabled}
                        onClick={() => onManageMonthly(budget)}
                        aria-label={`Manage monthly plan for ${budget.model}`}
                      >
                        Manage monthly plan
                      </Button>
                    ) : (
                      <span className="text-xs text-muted-foreground">Managed by a monthly plan</span>
                    ))}
                  {onEdit && !budget.recurrence_id && (
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={`Edit budget for ${budget.model}`}
                      disabled={disabled || budget.state === "expired"}
                      onClick={() => onEdit(budget)}
                    >
                      Edit
                    </Button>
                  )}
                  {onSchedule && !budget.recurrence_id && (
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={`Schedule next window for ${budget.model}`}
                      disabled={
                        disabled || !["active", "disabled"].includes(budget.state) || Boolean(budget.successor_id)
                      }
                      onClick={() => onSchedule(budget)}
                    >
                      Schedule next window
                    </Button>
                  )}
                </div>
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
