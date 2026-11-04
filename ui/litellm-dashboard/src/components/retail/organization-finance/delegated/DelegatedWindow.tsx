"use client";

import { useId, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { beijingTimeInput, managementInterval } from "../../management/time";

export default function DelegatedWindow({
  start,
  end,
  disabled,
  onApply,
}: {
  start: Date;
  end: Date;
  disabled: boolean;
  onApply: (start: Date, end: Date) => void;
}) {
  const id = useId();
  const [from, setFrom] = useState(beijingTimeInput(start.toISOString()));
  const [until, setUntil] = useState(beijingTimeInput(end.toISOString()));
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const result = managementInterval(from, until, true);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    if (result.interval) {
      setError(null);
      onApply(new Date(result.interval.start_at), new Date(result.interval.end_at));
    }
  };
  return (
    <form onSubmit={submit} className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-2">
          <Label htmlFor={`${id}-from`}>Accepted from (Beijing)</Label>
          <Input
            id={`${id}-from`}
            type="datetime-local"
            value={from}
            disabled={disabled}
            onChange={(event) => setFrom(event.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-until`}>Accepted before (Beijing)</Label>
          <Input
            id={`${id}-until`}
            type="datetime-local"
            value={until}
            disabled={disabled}
            onChange={(event) => setUntil(event.target.value)}
          />
        </div>
        <Button type="submit" variant="outline" disabled={disabled}>
          Apply window
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}
