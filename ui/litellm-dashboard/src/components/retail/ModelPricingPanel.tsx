"use client";

import { ShieldCheck, Tags } from "lucide-react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { LoadingState } from "@/components/shared/LoadingState";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { isProxyAdminRole } from "@/utils/roles";
import ModelPrices from "./ModelPrices";
import { upstreamSearchAllowanceNote } from "./expressionPricing";

export default function ModelPricingPanel() {
  const { accessToken, userRole, isAuthorized, isLoading } = useAuthorized();

  if (isLoading) return <LoadingState title="Loading model pricing" description="Verifying administrator access." />;
  if (!isAuthorized || !accessToken)
    return (
      <StateMessage
        role="alert"
        icon={<ShieldCheck />}
        title="Model pricing is unavailable"
        description="Sign in with an administrator account to manage site-wide base prices."
      />
    );
  if (!isProxyAdminRole(userRole))
    return (
      <StateMessage
        role="alert"
        icon={<ShieldCheck />}
        title="Administrator access required"
        description="Site-wide model base prices are managed by proxy administrators."
      />
    );

  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8">
      <PageHeader>
        <PageHeaderTitle>
          <Tags />
          Model pricing
        </PageHeaderTitle>
        <PageHeaderDescription>
          Site-wide base rates in USD per 1M tokens, including cache read and cache write duration prices. Customer
          charges use these rates with the applicable customer, plan, or resource pool discount. Dynamic rules can price
          context tiers, modalities, service tiers and time conditions.
        </PageHeaderDescription>
      </PageHeader>
      <p className="text-sm text-muted-foreground">{upstreamSearchAllowanceNote}</p>
      <ModelPrices accessToken={accessToken} enabled />
    </div>
  );
}
