"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, CircleAlert, CreditCard, Plus, RefreshCw, Search, Users } from "lucide-react";
import { parseAsInteger, parseAsString, useQueryStates } from "nuqs";
import { FormEvent, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import CopyButton from "@/components/shared/CopyButton";
import { LoadingState } from "@/components/shared/LoadingState";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "@/lib/toast";
import { isProxyAdminRole } from "@/utils/roles";
import {
  formatMoney,
  isPositiveAmount,
  PageResult,
  PricingPlan,
  retailKeys,
  retailRequest,
  WalletAccount,
} from "./api";

import PendingReservations from "./PendingReservations";
import ModelPrices from "./ModelPrices";

interface CreditForm {
  user_id: string;
  amount: string;
  reference: string;
  description: string;
}

const emptyCredit: CreditForm = { user_id: "", amount: "", reference: "", description: "" };
const pageSize = 25;

export default function AdminWalletsPanel() {
  const { accessToken, userRole, isAuthorized, isLoading: authLoading } = useAuthorized();
  const isAdmin = isProxyAdminRole(userRole);
  const client = useQueryClient();
  const [filters, setFilters] = useQueryStates({
    page: parseAsInteger.withDefault(1),
    query: parseAsString.withDefault(""),
  });
  const [search, setSearch] = useState(filters.query);
  const [creditOpen, setCreditOpen] = useState(false);
  const [credit, setCredit] = useState<CreditForm>(emptyCredit);
  const [attempt, setAttempt] = useState<{ body: string; key: string } | null>(null);
  const [creditValidation, setCreditValidation] = useState<string | null>(null);
  const [planOpen, setPlanOpen] = useState(false);
  const [newPlan, setNewPlan] = useState({ id: "", name: "", multiplier: "1" });
  const [planValidation, setPlanValidation] = useState<string | null>(null);
  const [planTarget, setPlanTarget] = useState<WalletAccount | null>(null);
  const [selectedPlan, setSelectedPlan] = useState<string | null>(null);
  const page = Math.max(1, filters.page);

  const wallets = useQuery({
    queryKey: retailKeys.wallets(filters.query, page),
    queryFn: () =>
      retailRequest<PageResult<WalletAccount>>(
        `/admin/wallets?query=${encodeURIComponent(filters.query)}&limit=${pageSize}&offset=${(page - 1) * pageSize}`,
        accessToken,
      ),
    enabled: isAuthorized && isAdmin && Boolean(accessToken),
  });
  const plans = useQuery({
    queryKey: retailKeys.plans,
    queryFn: () => retailRequest<{ items: PricingPlan[] }>("/plans", accessToken),
    enabled: isAuthorized && isAdmin && Boolean(accessToken),
  });
  const creditMutation = useMutation({
    mutationFn: ({ body, key }: { body: string; key: string }) =>
      retailRequest<WalletAccount>("/admin/credit", accessToken, {
        method: "POST",
        body,
        headers: { "Idempotency-Key": key },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setCreditOpen(false);
      setCredit(emptyCredit);
      setAttempt(null);
      toast.success("Credit recorded successfully");
    },
  });
  const planMutation = useMutation({
    mutationFn: () =>
      retailRequest<PricingPlan>("/admin/plans", accessToken, { method: "POST", body: JSON.stringify(newPlan) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setPlanOpen(false);
      setNewPlan({ id: "", name: "", multiplier: "1" });
      toast.success("Pricing plan saved");
    },
  });
  const assignmentMutation = useMutation({
    mutationFn: () =>
      retailRequest<WalletAccount>(
        `/admin/wallets/${encodeURIComponent(planTarget?.user_id ?? "")}/plan`,
        accessToken,
        { method: "PUT", body: JSON.stringify({ plan_id: selectedPlan }) },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setPlanTarget(null);
      toast.success("Pricing plan assigned");
    },
  });
  const openCredit = (userId = "") => {
    setCredit({ ...emptyCredit, user_id: userId });
    setCreditValidation(null);
    creditMutation.reset();
    setCreditOpen(true);
  };

  const submitCredit = (event: FormEvent) => {
    event.preventDefault();
    if (!isPositiveAmount(credit.amount)) {
      setCreditValidation("Enter a positive USD amount, with no more than 9 decimal places.");
      return;
    }
    if (!credit.user_id.trim() || !credit.reference.trim()) {
      setCreditValidation("An account ID and payment reference are required.");
      return;
    }
    setCreditValidation(null);
    const body = JSON.stringify({
      ...credit,
      user_id: credit.user_id.trim(),
      reference: credit.reference.trim(),
      description: credit.description.trim(),
    });
    const currentAttempt = attempt?.body === body ? attempt : { body, key: crypto.randomUUID() };
    setAttempt(currentAttempt);
    creditMutation.mutate(currentAttempt);
  };

  const submitPlan = (event: FormEvent) => {
    event.preventDefault();
    if (
      !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(newPlan.id) ||
      !newPlan.name.trim() ||
      !isPositiveAmount(newPlan.multiplier)
    ) {
      setPlanValidation("Use a lowercase plan ID, a name, and a positive pricing multiplier.");
      return;
    }
    setPlanValidation(null);
    planMutation.mutate();
  };

  if (!authLoading && isAuthorized && !isAdmin)
    return (
      <StateMessage
        role="alert"
        icon={<CircleAlert className="size-5" />}
        title="Administrator access required"
        description="Only proxy administrators can credit wallets or manage pricing plans."
      />
    );
  const total = wallets.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Users />
            Customer wallets
          </PageHeaderTitle>
          <PageHeaderDescription>
            Manage prepaid balances, record manual credits, and assign shared pricing plans.
          </PageHeaderDescription>
        </PageHeader>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={wallets.isFetching}
            onClick={() => void wallets.refetch()}
            aria-label="Refresh wallets"
          >
            <RefreshCw className={wallets.isFetching ? "animate-spin" : ""} />
          </Button>
          <Button onClick={() => openCredit()}>
            <Plus />
            Add credit
          </Button>
        </div>
      </div>

      <section className="overflow-hidden rounded-xl border">
        <form
          className="flex flex-wrap items-center gap-2 border-b p-4"
          onSubmit={(event) => {
            event.preventDefault();
            void setFilters({ query: search.trim(), page: 1 });
          }}
        >
          <div className="relative min-w-60 max-w-sm flex-1">
            <Search className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" />
            <Input
              aria-label="Search account ID"
              placeholder="Search account ID..."
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              className="pl-9"
            />
          </div>
          <Button variant="outline" type="submit">
            Search
          </Button>
          {filters.query && (
            <Button
              variant="ghost"
              onClick={() => {
                setSearch("");
                void setFilters({ query: "", page: 1 });
              }}
            >
              Clear
            </Button>
          )}
          <span className="ml-auto text-xs text-muted-foreground">
            {total} account{total === 1 ? "" : "s"}
          </span>
        </form>
        {wallets.isPending ? (
          <LoadingState title="Loading wallets" description="Retrieving customer balances." />
        ) : wallets.error ? (
          <StateMessage
            role="alert"
            tone="destructive"
            icon={<CircleAlert className="size-5" />}
            title="Unable to load wallets"
            description={wallets.error.message}
          />
        ) : wallets.data?.items.length === 0 ? (
          <StateMessage
            role="status"
            icon={<CreditCard className="size-5" />}
            title="No customer wallets found"
            description="Registered customers appear here. You can also credit an existing account by its account ID."
          />
        ) : (
          <Table>
            <TableHeader className="bg-muted/40">
              <TableRow>
                <TableHead className="pl-5">Account</TableHead>
                <TableHead>Pricing plan</TableHead>
                <TableHead className="text-right">Available (USD)</TableHead>
                <TableHead className="text-right">Reserved</TableHead>
                <TableHead className="text-right">Lifetime spend</TableHead>
                <TableHead>Pending</TableHead>
                <TableHead className="pr-5 text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {wallets.data?.items.map((wallet) => (
                <TableRow key={wallet.user_id}>
                  <TableCell className="pl-5">
                    <div className="flex items-center gap-2">
                      <span className="max-w-64 truncate font-mono text-xs" title={wallet.user_id}>
                        {wallet.user_id}
                      </span>
                      <CopyButton value={wallet.user_id} label="Copy account ID" />
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{wallet.pricing_plan.name}</Badge>
                  </TableCell>
                  <TableCell className="text-right font-medium tabular-nums">{formatMoney(wallet.available)}</TableCell>
                  <TableCell className="text-right text-muted-foreground tabular-nums">
                    {formatMoney(wallet.reserved)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatMoney(wallet.total_spent)}</TableCell>
                  <TableCell>{wallet.pending_requests}</TableCell>
                  <TableCell className="pr-5">
                    <div className="flex justify-end gap-1">
                      <Button size="xs" variant="outline" onClick={() => openCredit(wallet.user_id)}>
                        Add credit
                      </Button>
                      <Button
                        size="xs"
                        variant="ghost"
                        onClick={() => {
                          setSelectedPlan(wallet.pricing_plan.id);
                          assignmentMutation.reset();
                          setPlanTarget(wallet);
                        }}
                      >
                        Set plan
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <div className="flex items-center justify-between gap-3 border-t px-5 py-3 text-xs text-muted-foreground">
          <span>Balances are updated after each committed transaction</span>
          <div className="flex items-center gap-3">
            <span>
              Page {page} of {pageCount}
            </span>
            <Button
              size="icon-xs"
              variant="outline"
              aria-label="Previous wallets page"
              disabled={page <= 1 || wallets.isFetching}
              onClick={() => void setFilters({ page: page - 1 })}
            >
              <ChevronLeft />
            </Button>
            <Button
              size="icon-xs"
              variant="outline"
              aria-label="Next wallets page"
              disabled={page >= pageCount || wallets.isFetching}
              onClick={() => void setFilters({ page: page + 1 })}
            >
              <ChevronRight />
            </Button>
          </div>
        </div>
      </section>

      <Card className="shadow-none">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <CardTitle>Pricing plans</CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setNewPlan({ id: "", name: "", multiplier: "1" });
              planMutation.reset();
              setPlanValidation(null);
              setPlanOpen(true);
            }}
          >
            <Plus />
            New plan
          </Button>
        </CardHeader>
        <CardContent>
          <p className="mb-4 text-sm text-muted-foreground">
            Reuse a plan across customers. Updating a shared plan applies to new requests. Existing reservations keep
            their original pricing snapshot.
          </p>
          {plans.error ? (
            <Alert variant="error">
              <CircleAlert />
              <AlertTitle>{plans.error.message}</AlertTitle>
            </Alert>
          ) : plans.isPending ? (
            <p className="text-sm text-muted-foreground" role="status">
              Loading pricing plans...
            </p>
          ) : (
            <div className="flex flex-wrap gap-3">
              {plans.data?.items.map((plan) => (
                <div key={plan.id} className="min-w-44 rounded-lg border px-4 py-3">
                  <p className="font-medium">{plan.name}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {plan.id} · {plan.multiplier}× sales price
                  </p>
                  <Button
                    variant="link"
                    size="xs"
                    className="mt-2 px-0"
                    onClick={() => {
                      setNewPlan(plan);
                      planMutation.reset();
                      setPlanValidation(null);
                      setPlanOpen(true);
                    }}
                  >
                    Edit plan
                  </Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <ModelPrices accessToken={accessToken} enabled={isAuthorized && isAdmin} />
      <PendingReservations accessToken={accessToken} enabled={isAuthorized && isAdmin} />

      <Dialog
        open={creditOpen}
        onOpenChange={(open) => {
          if (!creditMutation.isPending) setCreditOpen(open);
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Add wallet credit</DialogTitle>
            <DialogDescription>Record a confirmed payment or an approved account credit.</DialogDescription>
          </DialogHeader>
          <form onSubmit={submitCredit} className="flex flex-col gap-5">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="credit-user">Account ID</FieldLabel>
                <Input
                  id="credit-user"
                  value={credit.user_id}
                  onChange={(event) => setCredit({ ...credit, user_id: event.target.value })}
                  required
                  maxLength={255}
                  disabled={creditMutation.isPending}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="credit-amount">Credit amount (USD)</FieldLabel>
                <Input
                  id="credit-amount"
                  inputMode="decimal"
                  placeholder="25.00"
                  value={credit.amount}
                  onChange={(event) => setCredit({ ...credit, amount: event.target.value })}
                  required
                  disabled={creditMutation.isPending}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="credit-reference">Payment reference</FieldLabel>
                <Input
                  id="credit-reference"
                  placeholder="Bank transfer or approval reference"
                  value={credit.reference}
                  onChange={(event) => setCredit({ ...credit, reference: event.target.value })}
                  required
                  maxLength={128}
                  disabled={creditMutation.isPending}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="credit-description">Description (optional)</FieldLabel>
                <Input
                  id="credit-description"
                  value={credit.description}
                  onChange={(event) => setCredit({ ...credit, description: event.target.value })}
                  maxLength={500}
                  disabled={creditMutation.isPending}
                />
              </Field>
            </FieldGroup>
            {(creditValidation || creditMutation.error) && (
              <Alert variant="error">
                <CircleAlert />
                <AlertTitle>Credit was not confirmed</AlertTitle>
                <AlertDescription>{creditValidation || creditMutation.error?.message}</AlertDescription>
              </Alert>
            )}
            <p className="text-xs leading-relaxed text-muted-foreground">
              This adds funds immediately and records an audit entry. If the result is uncertain, retry without changing
              the fields to avoid recording the same credit twice.
            </p>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={creditMutation.isPending}
                onClick={() => setCreditOpen(false)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={creditMutation.isPending}>
                {creditMutation.isPending ? "Recording credit..." : "Confirm credit"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={planOpen}
        onOpenChange={(open) => {
          if (!planMutation.isPending) setPlanOpen(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save pricing plan</DialogTitle>
            <DialogDescription>Customers assigned to this plan share the same pricing multiplier.</DialogDescription>
          </DialogHeader>
          <form onSubmit={submitPlan} className="flex flex-col gap-5">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="plan-id">Plan ID</FieldLabel>
                <Input
                  id="plan-id"
                  placeholder="standard"
                  value={newPlan.id}
                  onChange={(event) => setNewPlan({ ...newPlan, id: event.target.value })}
                  required
                  maxLength={64}
                  disabled={planMutation.isPending}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="plan-name">Plan name</FieldLabel>
                <Input
                  id="plan-name"
                  placeholder="Standard"
                  value={newPlan.name}
                  onChange={(event) => setNewPlan({ ...newPlan, name: event.target.value })}
                  required
                  maxLength={100}
                  disabled={planMutation.isPending}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="plan-multiplier">Pricing multiplier</FieldLabel>
                <Input
                  id="plan-multiplier"
                  inputMode="decimal"
                  value={newPlan.multiplier}
                  onChange={(event) => setNewPlan({ ...newPlan, multiplier: event.target.value })}
                  required
                  disabled={planMutation.isPending}
                />
                <p className="text-xs text-muted-foreground">Customer charge = model sales price × multiplier</p>
              </Field>
            </FieldGroup>
            {(planValidation || planMutation.error) && (
              <Alert variant="error">
                <CircleAlert />
                <AlertTitle>{planValidation || planMutation.error?.message}</AlertTitle>
              </Alert>
            )}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setPlanOpen(false)}
                disabled={planMutation.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={planMutation.isPending}>
                {planMutation.isPending ? "Saving..." : "Save plan"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={planTarget !== null}
        onOpenChange={(open) => {
          if (!open && !assignmentMutation.isPending) setPlanTarget(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Assign pricing plan</DialogTitle>
            <DialogDescription className="break-all">{planTarget?.user_id}</DialogDescription>
          </DialogHeader>
          <Field>
            <FieldLabel htmlFor="assigned-plan">Pricing plan</FieldLabel>
            <Select
              items={(plans.data?.items ?? []).map((plan) => ({ value: plan.id, label: plan.name }))}
              value={selectedPlan}
              onValueChange={setSelectedPlan}
              disabled={assignmentMutation.isPending}
            >
              <SelectTrigger id="assigned-plan" className="w-full">
                <SelectValue placeholder="Select a plan" />
              </SelectTrigger>
              <SelectContent>
                {plans.data?.items.map((plan) => (
                  <SelectItem key={plan.id} value={plan.id}>
                    {plan.name} ({plan.multiplier}×)
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          {assignmentMutation.error && (
            <Alert variant="error">
              <CircleAlert />
              <AlertTitle>{assignmentMutation.error.message}</AlertTitle>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={assignmentMutation.isPending} onClick={() => setPlanTarget(null)}>
              Cancel
            </Button>
            <Button
              disabled={!selectedPlan || assignmentMutation.isPending || plans.isPending}
              onClick={() => assignmentMutation.mutate()}
            >
              {assignmentMutation.isPending ? "Saving..." : "Assign plan"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
