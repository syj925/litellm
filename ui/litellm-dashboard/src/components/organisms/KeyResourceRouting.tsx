"use client";

import { useEffect, useId } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Checkbox } from "@/components/ui/checkbox";
import { FieldLabel } from "@/components/ui/field";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { uiHref } from "@/utils/uiHref";
import { ResourceRoutingPolicy, resourcePolicyError, resourcePolicySummary } from "./resourceRoutingPolicy";
import { useKeyResourceCatalog } from "./useKeyResourceCatalog";

interface Props {
  accessToken: string | null;
  targetUserId: string | null;
  admin?: boolean;
  allowLegacy?: boolean;
  value: ResourceRoutingPolicy | null;
  onChange: (policy: ResourceRoutingPolicy | null) => void;
  onValidityChange: (valid: boolean) => void;
}

export default function KeyResourceRouting({
  accessToken,
  targetUserId,
  admin = false,
  allowLegacy = true,
  value,
  onChange,
  onValidityChange,
}: Props) {
  const id = useId();
  const catalog = useKeyResourceCatalog({ accessToken, targetUserId, admin });
  const pools = catalog.data?.items ?? [];
  const active = pools.filter((pool) => pool.enabled && pool.allowed_models.length > 0);
  const configured = catalog.data?.routing_enabled === true || value !== null;
  const error = resourcePolicyError(value, pools);
  const valid = !targetUserId
    ? value === null
    : !catalog.isPending && !catalog.error && (!configured || (value !== null && !error));
  useEffect(() => {
    onValidityChange(valid);
  }, [valid, onValidityChange]);
  useEffect(() => {
    if (catalog.data?.routing_enabled && value === null) onChange({ mode: "auto" });
  }, [catalog.data?.routing_enabled, value, onChange]);

  const changeGrant = (poolId: string, models: "*" | string[] | null) => {
    const existing = value?.mode === "custom" ? value.grants : [];
    const remaining = existing.filter((grant) => grant.pool_id !== poolId);
    onChange({ mode: "custom", grants: models === null ? remaining : [...remaining, { pool_id: poolId, models }] });
  };

  return (
    <section className="mb-6 rounded-lg border p-4" aria-label="Resource routing">
      <h3 className="text-base font-medium">Resource routing</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        Select pool and model pairs within this account&apos;s entitlement. The Models field lists only models available
        through the selected resource routing.
      </p>
      {!targetUserId ? (
        <p className="mt-3 text-sm text-muted-foreground">
          Choose a user-owned key to configure account resource pools. Unbound service accounts use native routing only
          when pool routing is disabled.
        </p>
      ) : catalog.isPending ? (
        <p role="status" className="mt-3 text-sm text-muted-foreground">
          Loading account resource pools...
        </p>
      ) : catalog.error ? (
        <Alert className="mt-3" variant="error">
          <AlertTitle>Resource pools could not be verified</AlertTitle>
          <AlertDescription>{catalog.error.message} Refresh before creating or changing a key.</AlertDescription>
        </Alert>
      ) : (
        <>
          {!catalog.data?.routing_enabled && value === null && (
            <p className="mt-3 rounded-md bg-muted p-3 text-sm">
              Account pool routing is disabled. This key currently uses legacy native routing. You can explicitly select
              a pool policy below.
            </p>
          )}
          <RadioGroup
            className="mt-4 grid gap-3 sm:grid-cols-3"
            value={value?.mode ?? "legacy"}
            onValueChange={(mode) => {
              if (mode === "auto") onChange({ mode: "auto" });
              if (mode === "fixed") onChange({ mode: "fixed", pool_id: "" });
              if (mode === "custom") onChange({ mode: "custom", grants: [] });
              if (mode === "legacy" && allowLegacy && !catalog.data?.routing_enabled) onChange(null);
            }}
          >
            {allowLegacy && !catalog.data?.routing_enabled && (
              <label className="flex cursor-pointer items-start gap-2 rounded-md border p-3">
                <RadioGroupItem value="legacy" />
                <span className="text-sm font-medium">Legacy native routing</span>
              </label>
            )}
            {(
              [
                ["auto", "Automatic", "All account-authorized pool/model pairs"],
                ["fixed", "Fixed pool", "One authorized pool, no cross-pool fallback"],
                ["custom", "Custom", "Choose allowed models separately for each pool"],
              ] as const
            ).map(([mode, label, hint]) => (
              <label key={mode} className="flex cursor-pointer items-start gap-2 rounded-md border p-3">
                <RadioGroupItem value={mode} />
                <span>
                  <span className="text-sm font-medium">{label}</span>
                  <span className="mt-1 block text-xs text-muted-foreground">{hint}</span>
                </span>
              </label>
            ))}
          </RadioGroup>
          {value?.mode === "fixed" && (
            <div className="mt-4">
              <FieldLabel htmlFor={`${id}-pool`}>Allowed resource pool</FieldLabel>
              <Select
                items={active.map((pool) => ({ value: pool.id, label: pool.name }))}
                value={value.pool_id || null}
                onValueChange={(pool_id) => {
                  if (pool_id) onChange({ mode: "fixed", pool_id });
                }}
              >
                <SelectTrigger id={`${id}-pool`} className="mt-2 w-full">
                  <SelectValue placeholder="Choose one pool" />
                </SelectTrigger>
                <SelectContent>
                  {active.map((pool) => (
                    <SelectItem key={pool.id} value={pool.id}>
                      {pool.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {value?.mode === "custom" && (
            <div className="mt-4 space-y-3">
              {pools.map((pool) => {
                const grant = value.grants.find((item) => item.pool_id === pool.id);
                const available = pool.enabled && pool.allowed_models.length > 0;
                return (
                  <div key={pool.id} className="rounded-md border p-3">
                    <label className="flex items-center gap-2 text-sm font-medium">
                      <Checkbox
                        aria-label={`Use pool ${pool.name}`}
                        checked={Boolean(grant)}
                        disabled={!available && !grant}
                        onCheckedChange={(checked) => changeGrant(pool.id, checked ? [] : null)}
                      />
                      {pool.name}
                      {!available && <span className="text-xs text-destructive">Unavailable</span>}
                    </label>
                    {grant && (
                      <div className="mt-3 space-y-2 pl-6">
                        <label className="flex items-center gap-2 text-sm">
                          <Checkbox
                            aria-label={`All allowed models in ${pool.name}`}
                            checked={grant.models === "*"}
                            disabled={!available}
                            onCheckedChange={(checked) => changeGrant(pool.id, checked ? "*" : [])}
                          />
                          All allowed models in this pool
                        </label>
                        {grant.models !== "*" &&
                          pool.allowed_models.map((model) => (
                            <label key={model} className="flex items-center gap-2 text-xs">
                              <Checkbox
                                aria-label={`${pool.name}: ${model}`}
                                checked={grant.models !== "*" && grant.models.includes(model)}
                                disabled={!available}
                                onCheckedChange={(checked) =>
                                  changeGrant(
                                    pool.id,
                                    checked
                                      ? [...(grant.models === "*" ? [] : grant.models), model]
                                      : (grant.models === "*" ? [] : grant.models).filter((item) => item !== model),
                                  )
                                }
                              />
                              {model}
                            </label>
                          ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {configured && error && (
            <p role="alert" className="mt-3 text-sm text-destructive">
              {error}
            </p>
          )}
          {value && (
            <p className="mt-3 text-xs text-muted-foreground">
              {resourcePolicySummary(value)} Disabled or unauthorized selections fail closed; they never silently use
              another pool.
            </p>
          )}
        </>
      )}
      <p className="mt-4 text-xs text-muted-foreground">
        Pool/model pairs stay paired, so choosing model A in one pool does not grant model A in every pool.{" "}
        <a
          className="underline underline-offset-4"
          href={admin ? uiHref("resource-pools") : `${uiHref("wallet")}#resource-pool-guide`}
        >
          Resource pools guide
        </a>
      </p>
    </section>
  );
}
