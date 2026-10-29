"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowDownLeft, ArrowUpRight, CircleAlert, Clock3, RefreshCw, Wallet } from "lucide-react";
import Link from "next/link";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import CopyButton from "@/components/shared/CopyButton";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { uiHref } from "@/utils/uiHref";
import {
  discountDescription,
  effectiveDiscount,
  formatMoney,
  PageResult,
  LedgerEntry,
  retailKeys,
  retailRequest,
  WalletAccount,
} from "./api";
import { LedgerTable } from "./BillingPanel";

export default function WalletPanel() {
  const { accessToken, userId, isAuthorized } = useAuthorized();
  const wallet = useQuery({
    queryKey: retailKeys.wallet(userId),
    queryFn: () => retailRequest<WalletAccount>("/wallet", accessToken),
    enabled: isAuthorized && Boolean(accessToken),
    refetchInterval: 30_000,
  });
  const activity = useQuery({
    queryKey: [...retailKeys.ledger(userId, "recent", 1)],
    queryFn: () => retailRequest<PageResult<LedgerEntry>>("/ledger?limit=5&offset=0", accessToken),
    enabled: isAuthorized && Boolean(accessToken),
  });

  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Wallet />
            Wallet
          </PageHeaderTitle>
          <PageHeaderDescription>
            Your prepaid balance and account activity. All amounts are in USD.
          </PageHeaderDescription>
        </PageHeader>
        <Button
          variant="outline"
          disabled={wallet.isFetching}
          onClick={() => {
            void wallet.refetch();
            void activity.refetch();
          }}
        >
          <RefreshCw className={wallet.isFetching ? "animate-spin" : ""} />
          Refresh
        </Button>
      </div>

      {wallet.isPending && (
        <LoadingState title="Loading your wallet" description="Retrieving your current balance and account details." />
      )}
      {wallet.error && (
        <Alert variant="error">
          <CircleAlert />
          <AlertTitle>Unable to load your wallet</AlertTitle>
          <AlertDescription>{wallet.error.message}</AlertDescription>
        </Alert>
      )}

      {wallet.data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {[
              {
                label: "Available balance",
                value: wallet.data.available,
                help: "Ready for new requests",
                icon: Wallet,
              },
              {
                label: "Reserved",
                value: wallet.data.reserved,
                help: `${wallet.data.pending_requests} request${wallet.data.pending_requests === 1 ? "" : "s"} awaiting settlement`,
                icon: Clock3,
              },
              {
                label: "Total credited",
                value: wallet.data.total_credited,
                help: "Lifetime account credits",
                icon: ArrowDownLeft,
              },
              {
                label: "Total spent",
                value: wallet.data.total_spent,
                help: "Settled customer charges",
                icon: ArrowUpRight,
              },
            ].map((item) => (
              <Card key={item.label} size="sm" className="shadow-none">
                <CardHeader className="flex flex-row items-center justify-between gap-2">
                  <CardDescription>{item.label}</CardDescription>
                  <item.icon className="size-4 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                  <p className="break-all text-2xl font-semibold tracking-tight tabular-nums">
                    {formatMoney(item.value)}
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground">{item.help}</p>
                </CardContent>
              </Card>
            ))}
          </div>

          <div className="grid gap-6 xl:grid-cols-[1.3fr_1fr]">
            <Card className="shadow-none">
              <CardHeader>
                <CardTitle>Add funds</CardTitle>
                <CardDescription>Contact your administrator to arrange a manual top-up.</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <p className="text-sm text-muted-foreground">
                  Share your account ID and payment reference with your administrator. Confirmed credits appear in your
                  wallet and billing history.
                </p>
                <div className="flex min-w-0 items-center justify-between gap-3 rounded-lg border bg-muted/30 p-3">
                  <div className="min-w-0">
                    <p className="mb-1 text-xs text-muted-foreground">Account ID</p>
                    <p className="break-all font-mono text-xs">{wallet.data.user_id}</p>
                  </div>
                  <CopyButton value={wallet.data.user_id} label="Copy account ID" />
                </div>
              </CardContent>
            </Card>
            <Card className="shadow-none">
              <CardHeader>
                <CardTitle>Account details</CardTitle>
                <CardDescription>Pricing and settlement for this account.</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-4 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-muted-foreground">Current balance</span>
                  <span className="font-medium tabular-nums">{formatMoney(wallet.data.balance)}</span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-muted-foreground">Discount plan</span>
                  <Badge variant="outline">{wallet.data.pricing_plan.name}</Badge>
                </div>
                <div className="flex items-start justify-between gap-3">
                  <span className="text-muted-foreground">Customer discount</span>
                  <div className="text-right">
                    <p className="font-medium">{discountDescription(effectiveDiscount(wallet.data))}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {wallet.data.discount_source === "customer" ? "Customer override" : "Inherited from your plan"}
                    </p>
                  </div>
                </div>
                <div className="border-t pt-3 text-xs leading-relaxed text-muted-foreground">
                  A reservation sets funds aside while a request runs. The final charge is recorded after settlement.
                  Released reservations become available again.
                </div>
                <Link href={uiHref("api-keys")} className="text-sm font-medium underline-offset-4 hover:underline">
                  Manage API keys <span aria-hidden="true">↗</span>
                </Link>
              </CardContent>
            </Card>
          </div>
        </>
      )}

      <section className="overflow-hidden rounded-xl border">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-4">
          <h2 className="text-base font-medium">Recent activity</h2>
          <Link href={uiHref("billing")} className="text-sm font-medium text-muted-foreground hover:text-foreground">
            View all transactions <span aria-hidden="true">↗</span>
          </Link>
        </div>
        <LedgerTable entries={activity.data?.items ?? []} loading={activity.isPending} error={activity.error} />
      </section>
    </div>
  );
}
