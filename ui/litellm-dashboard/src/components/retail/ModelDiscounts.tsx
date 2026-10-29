"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert } from "lucide-react";
import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "@/lib/toast";
import {
  discountDescription,
  discountToPercent,
  ModelDiscount,
  ModelDiscountList,
  modelDiscountSourceLabel,
  percentToDiscount,
  retailRequest,
} from "./api";
import { PoolCatalog, ResourcePool } from "./pools/types";

const legacyPool = "__legacy";
const poolQuery = (path: string, poolId: string) =>
  poolId === legacyPool ? path : `${path}?pool_id=${encodeURIComponent(poolId)}`;

function PoolSelector({
  pools,
  value,
  onChange,
  disabled = false,
}: {
  pools: { id: string; name: string }[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const options = [
    { value: legacyPool, label: "Legacy / unpooled" },
    ...pools.map((pool) => ({ value: pool.id, label: pool.name })),
  ];
  return (
    <Field className="max-w-sm">
      <FieldLabel htmlFor="discount-resource-pool">Resource pool</FieldLabel>
      <Select
        disabled={disabled}
        items={options}
        value={value}
        onValueChange={(next) => {
          if (next) onChange(next);
        }}
      >
        <SelectTrigger id="discount-resource-pool" className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  );
}

interface PoolDiscountDefault {
  discount: string | null;
  plan_discount?: string | null;
  pool_default_discount: string;
  effective_discount: string;
  discount_source: ModelDiscount["discount_source"];
}

export type DiscountScope = { kind: "customer" | "plan"; id: string; name: string };

function DiscountRow({ row, customer }: { row: ModelDiscount; customer: boolean }) {
  return (
    <>
      <TableCell className="min-w-48 max-w-72 break-all whitespace-normal font-medium">{row.model}</TableCell>
      <TableCell className="text-xs tabular-nums">{discountDescription(row.discount)}</TableCell>
      {customer && (
        <TableCell className="text-xs tabular-nums">
          {row.customer_discount ? `${discountToPercent(row.customer_discount)}% of reference` : "Inherits"}
        </TableCell>
      )}
      <TableCell className="text-xs tabular-nums">
        {row.plan_discount ? `${discountToPercent(row.plan_discount)}% of reference` : "No model discount"}
      </TableCell>
      <TableCell>
        <Badge variant="outline">{modelDiscountSourceLabel(row.discount_source)}</Badge>
      </TableCell>
    </>
  );
}

export function CustomerModelDiscounts({
  accessToken,
  userId,
  enabled,
}: {
  accessToken: string | null;
  userId: string | null;
  enabled: boolean;
}) {
  const [search, setSearch] = useState("");
  const [poolId, setPoolId] = useState<string | null>(null);
  const pools = useQuery({
    queryKey: ["retail", "pools", "self", userId],
    queryFn: () => retailRequest<PoolCatalog>("/pools", accessToken),
    enabled: enabled && Boolean(accessToken) && Boolean(userId),
  });
  const selectedPoolId =
    poolId ?? (pools.data?.routing_enabled && pools.data.items.length ? pools.data.items[0].id : legacyPool);
  const selectedPoolName =
    selectedPoolId === legacyPool
      ? "Legacy / unpooled"
      : pools.data?.items.find((pool) => pool.id === selectedPoolId)?.name ?? selectedPoolId;
  const pricing = useQuery({
    queryKey: ["retail", "model-discounts", "self", userId, selectedPoolId],
    queryFn: () => retailRequest<ModelDiscountList>(poolQuery("/model-discounts", selectedPoolId), accessToken),
    enabled: enabled && Boolean(accessToken) && Boolean(userId) && Boolean(pools.data),
  });
  const rows = pricing.data?.items.filter((row) => row.model.toLowerCase().includes(search.toLowerCase())) ?? [];
  return (
    <section className="overflow-hidden rounded-xl border">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b px-5 py-4">
        <div>
          <h2 className="text-base font-medium">Your model discounts</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Pricing is negotiated per resource pool and public model. Legacy rules are separate and are not copied into
            pools.
          </p>
        </div>
        <Input
          aria-label="Search your model discounts"
          placeholder="Search model..."
          className="max-w-sm"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>
      <div className="border-b p-4">
        <PoolSelector pools={pools.data?.items ?? []} value={selectedPoolId} onChange={setPoolId} />
      </div>
      {pools.isPending || (pricing.isPending && !pools.error) ? (
        <LoadingState title="Loading model discounts" description="Retrieving your effective model pricing." />
      ) : pricing.error || pools.error ? (
        <Alert variant="error">
          <CircleAlert />
          <AlertTitle>Unable to load model discounts</AlertTitle>
          <AlertDescription>{(pricing.error ?? pools.error)?.message}</AlertDescription>
        </Alert>
      ) : (
        <Table>
          <TableHeader className="bg-muted/40">
            <TableRow>
              <TableHead>Resource pool</TableHead>
              <TableHead>Public model</TableHead>
              <TableHead>Effective discount</TableHead>
              <TableHead>Your model override</TableHead>
              <TableHead>Plan model discount</TableHead>
              <TableHead>Source</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.model}>
                <TableCell className="text-xs">{selectedPoolName}</TableCell>
                <DiscountRow row={row} customer />
              </TableRow>
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                  No matching model discounts
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}
      <p className="border-t px-5 py-3 text-xs text-muted-foreground">
        Within a pool, customer-model and plan-model rules take priority over customer-pool, plan-pool, and resource
        pool defaults. Discounts do not grant model access.
      </p>
    </section>
  );
}

export default function ModelDiscountEditor({
  accessToken,
  scope,
  onClose,
}: {
  accessToken: string | null;
  scope: DiscountScope;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [percentage, setPercentage] = useState("");
  const [validation, setValidation] = useState<string | null>(null);
  const [selectedPoolId, setSelectedPoolId] = useState(legacyPool);
  const [defaultPercentage, setDefaultPercentage] = useState<string | null>(null);
  const basePath =
    scope.kind === "customer"
      ? `/admin/wallets/${encodeURIComponent(scope.id)}/model-discounts`
      : `/admin/plans/${encodeURIComponent(scope.id)}/model-discounts`;
  const path = poolQuery(basePath, selectedPoolId);
  const pools = useQuery({
    queryKey: ["retail", "pools", "admin"],
    queryFn: () => retailRequest<{ items: ResourcePool[] }>("/admin/pools", accessToken),
    enabled: Boolean(accessToken),
  });
  const selectedPool = pools.data?.items.find((pool) => pool.id === selectedPoolId);
  const defaultPath = poolQuery(
    scope.kind === "customer"
      ? `/admin/wallets/${encodeURIComponent(scope.id)}/pool-discount`
      : `/admin/plans/${encodeURIComponent(scope.id)}/pool-discount`,
    selectedPoolId,
  );
  const poolDefault = useQuery({
    queryKey: ["retail", "pool-discount", scope.kind, scope.id, selectedPoolId],
    queryFn: () => retailRequest<PoolDiscountDefault>(defaultPath, accessToken),
    enabled: Boolean(accessToken) && selectedPoolId !== legacyPool,
  });
  const defaultMutation = useMutation({
    mutationFn: (discount: string | null) =>
      retailRequest(defaultPath, accessToken, { method: "PUT", body: JSON.stringify({ discount }) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setDefaultPercentage(null);
      toast.success("Pool default discount updated");
    },
  });
  const discounts = useQuery({
    queryKey: ["retail", "model-discounts", scope.kind, scope.id, selectedPoolId],
    queryFn: () => retailRequest<ModelDiscountList>(path, accessToken),
    enabled: Boolean(accessToken),
  });
  const catalog = useQuery({
    queryKey: ["retail", "discount-models"],
    queryFn: () => retailRequest<{ models: string[] }>("/admin/discount-models", accessToken),
    enabled: Boolean(accessToken),
  });
  const mutation = useMutation({
    mutationFn: (discount: string | null) =>
      retailRequest<ModelDiscountList>(path, accessToken, {
        method: "PUT",
        body: JSON.stringify({ models: selected, discount }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setSelected([]);
      setValidation(null);
      toast.success("Model discounts updated");
    },
  });
  const rows = discounts.data?.items.filter((row) => row.model.toLowerCase().includes(search.toLowerCase())) ?? [];
  const configuredModels = selectedPoolId === legacyPool ? catalog.data?.models ?? [] : selectedPool?.models ?? [];
  const selectedIsCurrent = selected.every((model) => configuredModels.includes(model));
  const ownsOverride = (row: ModelDiscount) =>
    scope.kind === "customer" ? row.customer_discount != null : row.plan_discount != null;
  const selectedCanClear = selected.every(
    (model) =>
      configuredModels.includes(model) || discounts.data?.items.some((row) => row.model === model && ownsOverride(row)),
  );
  const busy = mutation.isPending || defaultMutation.isPending;
  const loading = discounts.isPending || catalog.isPending || pools.isPending;
  const error = discounts.error ?? catalog.error ?? pools.error;
  const hasLegacyAccount = discounts.data?.items.some((row) => row.discount_source === "legacy_customer");
  const hasLegacyPlan = discounts.data?.items.some((row) => row.discount_source === "legacy_plan");
  const submit = (clear: boolean) => {
    if (!selected.length || (clear ? !selectedCanClear : !selectedIsCurrent)) {
      setValidation("Select one or more configured models. No models are selected automatically.");
      return;
    }
    const discount = clear ? null : percentToDiscount(percentage);
    if (!clear && discount === null) {
      setValidation("Enter a percentage greater than 0 and at most 100, with up to 7 decimal places.");
      return;
    }
    setValidation(null);
    mutation.mutate(discount);
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-6xl">
        <DialogHeader>
          <DialogTitle>{scope.kind === "customer" ? "Customer model discounts" : "Plan model discounts"}</DialogTitle>
          <DialogDescription className="break-all">
            {scope.name} · {scope.id}
          </DialogDescription>
        </DialogHeader>
        <PoolSelector
          disabled={busy}
          pools={pools.data?.items ?? []}
          value={selectedPoolId}
          onChange={(value) => {
            setSelectedPoolId(value);
            setSelected([]);
            setPercentage("");
            setDefaultPercentage(null);
            setValidation(null);
            mutation.reset();
            defaultMutation.reset();
          }}
        />
        <p className="text-sm text-muted-foreground">
          Choose explicit public models, then set the percentage paid for only those models. 70 means pay 70% of each
          model&apos;s reference price. Unselected models keep their current rules.
        </p>
        {selectedPoolId === legacyPool ? (
          <p className="rounded-lg border bg-muted/20 p-3 text-xs leading-relaxed text-muted-foreground">
            Legacy / unpooled rules apply only to requests outside resource pool routing. These rules are not
            automatically reused for any new pool.
          </p>
        ) : (
          <div className="rounded-lg border bg-muted/20 p-4">
            <h3 className="text-sm font-medium">
              {scope.kind === "customer" ? "Customer" : "Shared plan"} default for {selectedPool?.name}
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Applies within this pool when neither customer-model nor shared-plan model rules apply.
            </p>
            {poolDefault.isPending ? (
              <p className="mt-3 text-xs text-muted-foreground">Loading pool pricing...</p>
            ) : poolDefault.error ? (
              <Alert variant="error">
                <AlertTitle>{poolDefault.error.message}</AlertTitle>
              </Alert>
            ) : (
              <>
                <div className="mt-4 flex flex-wrap items-end gap-2">
                  <Field className="max-w-xs flex-1">
                    <FieldLabel htmlFor="scoped-pool-default">Pool percentage of reference price (%)</FieldLabel>
                    <Input
                      id="scoped-pool-default"
                      inputMode="decimal"
                      placeholder="Inherit pool pricing"
                      value={
                        defaultPercentage ??
                        (poolDefault.data?.discount ? discountToPercent(poolDefault.data.discount) : "")
                      }
                      onChange={(event) => setDefaultPercentage(event.target.value)}
                      disabled={busy}
                    />
                  </Field>
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      const value = percentToDiscount(
                        defaultPercentage ??
                          (poolDefault.data?.discount ? discountToPercent(poolDefault.data.discount) : ""),
                      );
                      if (value === null) {
                        setValidation("Enter a pool percentage greater than 0 and at most 100.");
                        return;
                      }
                      setValidation(null);
                      defaultMutation.mutate(value);
                    }}
                  >
                    Save pool default
                  </Button>
                  <Button variant="outline" size="sm" disabled={busy} onClick={() => defaultMutation.mutate(null)}>
                    Use inherited default
                  </Button>
                </div>
                <p className="mt-3 text-xs text-muted-foreground">
                  Effective pool default:{" "}
                  {poolDefault.data ? discountDescription(poolDefault.data.effective_discount) : "..."} ·{" "}
                  {poolDefault.data ? modelDiscountSourceLabel(poolDefault.data.discount_source) : ""}
                </p>
              </>
            )}
            {defaultMutation.error && (
              <Alert variant="error">
                <AlertTitle>{defaultMutation.error.message}</AlertTitle>
              </Alert>
            )}
          </div>
        )}
        {(hasLegacyAccount || hasLegacyPlan) && (
          <Alert>
            <AlertTitle>
              {hasLegacyAccount
                ? "Legacy account-wide discount is still active"
                : "Legacy plan-wide discount is still active"}
            </AlertTitle>
            <AlertDescription>
              Select only the models covered by the negotiated discount before migrating. This editor preserves the
              existing legacy fallback until it is explicitly cleared.
            </AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Input
            aria-label="Search model discounts"
            className="min-w-56 flex-1"
            placeholder="Search public model..."
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <Button
            variant="outline"
            size="sm"
            disabled={loading || busy || Boolean(error)}
            onClick={() =>
              setSelected([
                ...new Set([
                  ...selected,
                  ...rows.filter((row) => configuredModels.includes(row.model)).map((row) => row.model),
                ]),
              ])
            }
          >
            Select visible models
          </Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setSelected([])}>
            Clear selection
          </Button>
        </div>
        {loading ? (
          <LoadingState
            title="Loading model discounts"
            description="Retrieving model rules and available public models."
          />
        ) : error ? (
          <Alert variant="error">
            <AlertTitle>Unable to load model discounts</AlertTitle>
            <AlertDescription>{error.message}</AlertDescription>
          </Alert>
        ) : (
          <div className="max-h-80 overflow-auto rounded-lg border">
            <Table>
              <TableHeader className="bg-muted/40">
                <TableRow>
                  <TableHead className="w-12">Select</TableHead>
                  <TableHead>Public model</TableHead>
                  <TableHead>Effective discount</TableHead>
                  {scope.kind === "customer" && <TableHead>Customer override</TableHead>}
                  <TableHead>Plan model discount</TableHead>
                  <TableHead>Source</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.model}>
                    <TableCell>
                      <Checkbox
                        aria-label={`Select model ${row.model}`}
                        checked={selected.includes(row.model)}
                        disabled={busy || (!configuredModels.includes(row.model) && !ownsOverride(row))}
                        onCheckedChange={(checked) =>
                          setSelected(
                            checked ? [...selected, row.model] : selected.filter((model) => model !== row.model),
                          )
                        }
                      />
                    </TableCell>
                    <DiscountRow row={row} customer={scope.kind === "customer"} />
                  </TableRow>
                ))}
                {rows.length === 0 && (
                  <TableRow>
                    <TableCell
                      colSpan={scope.kind === "customer" ? 6 : 5}
                      className="py-8 text-center text-muted-foreground"
                    >
                      No matching public models
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        )}
        <div className="flex flex-wrap items-end justify-between gap-4 rounded-lg border bg-muted/20 p-4">
          <Field className="max-w-sm flex-1">
            <FieldLabel htmlFor="model-discount-percentage">Pay percentage of reference price (%)</FieldLabel>
            <Input
              id="model-discount-percentage"
              inputMode="decimal"
              placeholder="70"
              value={percentage}
              onChange={(event) => setPercentage(event.target.value)}
              disabled={busy}
            />
            <p className="text-xs text-muted-foreground">
              Applied to {selected.length} explicitly selected model{selected.length === 1 ? "" : "s"}. This does not
              change access or rate limits.
            </p>
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={!selected.length || busy || loading || Boolean(error) || !selectedCanClear}
              onClick={() => submit(true)}
            >
              Remove selected overrides
            </Button>
            <Button
              disabled={!selected.length || busy || loading || Boolean(error) || !selectedIsCurrent}
              onClick={() => submit(false)}
            >
              {busy ? "Saving..." : `Apply to ${selected.length} models`}
            </Button>
          </div>
          {selected.length > 0 && !selectedIsCurrent && (
            <p className="text-xs text-muted-foreground">
              Your selection includes inactive models. Existing overrides can be removed, but new discounts require
              configured public models.
            </p>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          {selectedPoolId !== legacyPool
            ? "Removing a model override restores the next eligible pricing rule within this resource pool."
            : scope.kind === "customer"
              ? "Removing customer overrides restores the shared plan's model discount, or the standard reference price when no model rule exists."
              : "Removing plan model overrides restores the standard reference price for these models."}{" "}
          {selectedPoolId === legacyPool
            ? "Existing legacy account/plan fallbacks are preserved and labeled."
            : "Pool pricing inherits customer-pool, plan-pool, and resource pool defaults after model overrides are removed."}{" "}
          New requests use the updated rule; historical bills retain their original snapshot.
        </p>
        {(validation || mutation.error) && (
          <Alert variant="error">
            <AlertTitle>Model discounts were not saved</AlertTitle>
            <AlertDescription>{validation ?? mutation.error?.message}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
