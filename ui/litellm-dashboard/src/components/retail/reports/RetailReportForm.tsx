"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileDown } from "lucide-react";
import { useId, useRef, useState, type FormEvent } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { retailRequest } from "../api";
import { managementAttempt, type IdempotencyAttempt } from "../management/ManagementShared";
import { beijingTimeInput, managementInterval } from "../management/time";
import type { CreateRetailReport, RetailReport } from "./types";

type ReportKind = NonNullable<CreateRetailReport["kind"]> | "all";

export default function RetailReportForm({
  accessToken,
  admin,
  onCreated,
}: {
  accessToken: string;
  admin: boolean;
  onCreated: (report: RetailReport) => void;
}) {
  const formId = useId();
  const client = useQueryClient();
  const [from, setFrom] = useState("");
  const [until, setUntil] = useState("");
  const [kind, setKind] = useState<ReportKind>("all");
  const [accountId, setAccountId] = useState("");
  const [validation, setValidation] = useState("");
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const create = useMutation({
    mutationFn: (payload: CreateRetailReport) =>
      retailRequest<RetailReport>("/reports", accessToken, {
        method: "POST",
        headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
        body: JSON.stringify(payload),
      }),
    onSuccess: (report) => {
      attempt.current = null;
      void client.invalidateQueries({ queryKey: ["retail", "reports"] });
      onCreated(report);
    },
  });
  const setRecent = (days: number) => {
    const now = Date.now();
    setFrom(beijingTimeInput(new Date(now - days * 24 * 60 * 60 * 1000).toISOString()));
    setUntil(beijingTimeInput(new Date(now).toISOString()));
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const result = managementInterval(from, until, true);
    if (!result.ok) {
      setValidation(result.error);
      return;
    }
    if (!result.interval || create.isPending) return;
    setValidation("");
    const payload: CreateRetailReport = {
      ...result.interval,
      currency: "USD",
      timezone: "Asia/Shanghai",
      ...(kind === "all" ? {} : { kind }),
      ...(admin && accountId.trim() ? { account_id: accountId.trim() } : {}),
    };
    create.mutate(payload);
  };
  return (
    <section className="space-y-5 rounded-xl border p-5" aria-labelledby={`${formId}-title`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id={`${formId}-title`} className="font-semibold">
            Create a wallet report
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Choose a fixed time window of up to 31 days. The report captures a snapshot for repeatable downloads.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => setRecent(7)} disabled={create.isPending}>
            Last 7 days
          </Button>
          <Button variant="outline" size="sm" onClick={() => setRecent(30)} disabled={create.isPending}>
            Last 30 days
          </Button>
        </div>
      </div>
      <form onSubmit={submit} className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <div className="space-y-2">
            <Label htmlFor={`${formId}-from`}>From (Beijing time)</Label>
            <Input
              id={`${formId}-from`}
              type="datetime-local"
              value={from}
              onChange={(event) => setFrom(event.target.value)}
              required
              disabled={create.isPending}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-until`}>Until (exclusive, Beijing time)</Label>
            <Input
              id={`${formId}-until`}
              type="datetime-local"
              value={until}
              onChange={(event) => setUntil(event.target.value)}
              required
              disabled={create.isPending}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-kind`}>Transaction type</Label>
            <select
              id={`${formId}-kind`}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={kind}
              disabled={create.isPending}
              onChange={(event) => setKind(event.target.value as ReportKind)}
            >
              <option value="all">All transactions</option>
              <option value="credit">Credits</option>
              <option value="charge">Usage charges</option>
              <option value="release">Released reservations</option>
            </select>
          </div>
          {admin && (
            <div className="space-y-2">
              <Label htmlFor={`${formId}-account`}>Account ID (optional)</Label>
              <Input
                id={`${formId}-account`}
                value={accountId}
                onChange={(event) => setAccountId(event.target.value)}
                placeholder="Your own account by default"
                maxLength={191}
                disabled={create.isPending}
                autoComplete="off"
              />
            </div>
          )}
        </div>
        {validation && (
          <p role="alert" className="text-sm text-destructive">
            {validation}
          </p>
        )}
        {create.error && (
          <Alert variant="error">
            <AlertTitle>Unable to create report</AlertTitle>
            <AlertDescription>{create.error.message}</AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="max-w-3xl text-xs leading-relaxed text-muted-foreground">
            Totals describe retail wallet transactions in USD. Released reservations are distinct from refunds. Pending
            or unresolved requests are reported separately from settled charges.
          </p>
          <Button type="submit" disabled={create.isPending || !from || !until}>
            <FileDown />
            {create.isPending ? "Creating..." : "Create report"}
          </Button>
        </div>
      </form>
    </section>
  );
}
