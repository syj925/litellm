"use client";

import { CircleAlert } from "lucide-react";
import { useState } from "react";
import { getProxyBaseUrl } from "@/components/networking";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
import { getReturnUrl } from "@/utils/returnUrlUtils";
import { oidcErrorMessage, oidcGatewayBase, oidcReturnPath } from "./oidcLoginInput";
import { useOrganizationOidcStatus } from "./useOrganizationOidcStatus";

function AvailableOrganizationLogin({ base, disabled }: { base: string; disabled: boolean }) {
  const status = useOrganizationOidcStatus(base);
  const visible = !status.error && status.isFetchedAfterMount ? status.data : undefined;
  const startLogin = () => {
    const target = oidcReturnPath(getReturnUrl(), window.location.origin);
    const destination = new URL(`${base}/retail/auth/oidc/start?return_to=${encodeURIComponent(target)}`);
    window.location.assign(destination.href);
  };
  if (!visible?.enabled) return null;
  if (!visible.available)
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Organization sign-in is temporarily unavailable. You can use your username and password.
      </p>
    );
  return (
    <div className="space-y-2">
      <Button
        type="button"
        variant="outline"
        size="lg"
        className="h-auto min-h-10 w-full whitespace-normal py-2"
        disabled={disabled || status.isFetching}
        onClick={startLogin}
      >
        <span className="min-w-0 break-words">{visible.label.trim() || "Continue with organization account"}</span>
      </Button>
      <p className="text-sm text-muted-foreground">Console access. Use Virtual Keys for model API calls.</p>
    </div>
  );
}

export function OrganizationOidcError() {
  const [message] = useState(() => (typeof window === "undefined" ? null : oidcErrorMessage(window.location.search)));
  if (!message) return null;
  return (
    <Alert variant="error">
      <CircleAlert />
      <AlertTitle>Unable to sign in with your organization</AlertTitle>
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  );
}

export default function OrganizationOidcLogin({ supported, disabled }: { supported: boolean; disabled: boolean }) {
  if (!supported || typeof window === "undefined") return null;
  const base = oidcGatewayBase(getProxyBaseUrl(), window.location.origin);
  if (base === null) return null;
  return <AvailableOrganizationLogin key={base} base={base} disabled={disabled} />;
}
