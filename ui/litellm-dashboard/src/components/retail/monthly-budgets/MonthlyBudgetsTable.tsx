import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatMoney } from "../api";
import { monthlyTimestamp } from "./monthlyBudgetInput";
import type { MonthlyBudget } from "./types";

function planState(plan: MonthlyBudget): string {
  if (plan.state === "stopping") return "Stops at month end";
  if (plan.state === "stopped") return "Stopped";
  if (plan.state === "scheduled") return "Scheduled";
  return plan.current_window_ready ? "Active" : "Current window unavailable";
}

export default function MonthlyBudgetsTable<T extends MonthlyBudget>({
  items,
  disabled,
  onEdit,
  onStop,
}: {
  items: T[];
  disabled?: boolean;
  onEdit?: (plan: T) => void;
  onStop?: (plan: T) => void;
}) {
  const managing = Boolean(onEdit || onStop);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-5">Public model</TableHead>
          <TableHead>Plan schedule</TableHead>
          <TableHead className="text-right">Planned monthly limit (USD)</TableHead>
          <TableHead>Status</TableHead>
          {managing && (
            <TableHead className="pr-5">
              <span className="sr-only">Actions</span>
            </TableHead>
          )}
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((plan) => {
          const changeable = ["scheduled", "active"].includes(plan.state);
          return (
            <TableRow key={plan.id}>
              <TableCell className="max-w-56 break-all pl-5 font-medium">{plan.model}</TableCell>
              <TableCell className="min-w-48 text-xs">
                <div>{plan.timezone}</div>
                <div className="mt-1 text-muted-foreground">From {monthlyTimestamp(plan.starts_at, plan.timezone)}</div>
                {plan.stops_at && (
                  <div className="mt-1 text-muted-foreground">
                    Until {monthlyTimestamp(plan.stops_at, plan.timezone)} (exclusive)
                  </div>
                )}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                <div>{formatMoney(plan.limit_usd)}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  From {monthlyTimestamp(plan.limit_effective_from, plan.timezone)}
                </div>
              </TableCell>
              <TableCell className="max-w-56">
                <Badge variant="outline">{planState(plan)}</Badge>
                {["active", "stopping"].includes(plan.state) && !plan.current_window_ready && (
                  <p className="mt-2 text-xs text-destructive">
                    New requests remain blocked until this month’s window is ready.
                  </p>
                )}
              </TableCell>
              {managing && (
                <TableCell className="pr-5 text-right">
                  <div className="flex flex-col items-end gap-2">
                    {onEdit && changeable && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={disabled}
                        onClick={() => onEdit(plan)}
                        aria-label={`Change monthly limit for ${plan.model}`}
                      >
                        Change future limit
                      </Button>
                    )}
                    {onStop && changeable && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={disabled}
                        onClick={() => onStop(plan)}
                        aria-label={`Stop monthly plan for ${plan.model}`}
                      >
                        {plan.state === "scheduled" ? "Cancel plan" : "Stop at month end"}
                      </Button>
                    )}
                  </div>
                </TableCell>
              )}
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
