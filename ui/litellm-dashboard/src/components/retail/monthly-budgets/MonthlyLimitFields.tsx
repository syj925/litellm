import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { firstAllowedMonth } from "./monthlyBudgetInput";
import type { MonthlyTimezone } from "./types";

export default function MonthlyLimitFields({
  id,
  creating,
  amount,
  timezone,
  month,
  openedAt,
  busy,
  onAmount,
  onTimezone,
  onMonth,
}: {
  id: string;
  creating: boolean;
  amount: string;
  timezone: MonthlyTimezone;
  month: string;
  openedAt: number;
  busy: boolean;
  onAmount: (value: string) => void;
  onTimezone: (value: MonthlyTimezone) => void;
  onMonth: (value: string) => void;
}) {
  return (
    <div className="grid gap-5 sm:grid-cols-2">
      <div className="space-y-2">
        <Label htmlFor={`${id}-amount`}>Monthly limit (USD)</Label>
        <Input
          id={`${id}-amount`}
          inputMode="decimal"
          value={amount}
          maxLength={30}
          disabled={busy}
          onChange={(event) => onAmount(event.target.value)}
          required
          autoComplete="off"
        />
      </div>
      {creating && (
        <>
          <div className="space-y-2">
            <Label htmlFor={`${id}-timezone`}>Calendar timezone</Label>
            <select
              id={`${id}-timezone`}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={timezone}
              disabled={busy}
              onChange={(event) => {
                if (event.target.value === "UTC" || event.target.value === "Asia/Shanghai")
                  onTimezone(event.target.value);
              }}
            >
              <option value="Asia/Shanghai">Asia/Shanghai</option>
              <option value="UTC">UTC</option>
            </select>
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${id}-month`}>First full month</Label>
            <Input
              id={`${id}-month`}
              type="month"
              min={firstAllowedMonth(timezone, openedAt)}
              value={month}
              disabled={busy}
              onChange={(event) => onMonth(event.target.value)}
              required
            />
          </div>
        </>
      )}
    </div>
  );
}
