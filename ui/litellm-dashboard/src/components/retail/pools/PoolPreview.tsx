"use client";

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Route } from "lucide-react";
import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { discountDescription, formatMoney, ModelDiscountSource, modelDiscountSourceLabel, retailRequest } from "../api";
import { PoolBinding, ResourcePool } from "./types";
import { cacheWritePriceRows } from "../cachePricing";

interface RoutePreview {
  pool_id: string;
  pool_name: string;
  allowed_deployment_ids: string[];
  bindings: PoolBinding[];
  settings_version: number;
  eligible_count?: number;
  discount?: string;
  discount_source?: ModelDiscountSource;
  reference_prices?: {
    input_per_million: string;
    output_per_million: string;
    cache_read_per_million: string;
    cache_write_per_million: string;
    cache_write_tiers?: Record<string, string>;
  } | null;
}

export default function PoolPreview({
  accessToken,
  pool,
  onClose,
}: {
  accessToken: string | null;
  pool: ResourcePool;
  onClose: () => void;
}) {
  const [userId, setUserId] = useState("");
  const [model, setModel] = useState<string | null>(null);
  const [mode, setMode] = useState<"fixed" | "auto">("fixed");
  const preview = useMutation({
    mutationFn: () =>
      retailRequest<RoutePreview>("/pools/preview", accessToken, {
        method: "POST",
        body: JSON.stringify({
          ...(userId.trim() ? { user_id: userId.trim() } : {}),
          model,
          policy: mode === "fixed" ? { mode, pool_id: pool.id } : { mode },
        }),
      }),
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !preview.isPending) onClose();
      }}
    >
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Preview resource pool routing</DialogTitle>
          <DialogDescription>
            Check account access and eligible deployments without sending a model request or charging a wallet.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field>
            <FieldLabel htmlFor="preview-user">Customer account ID (optional)</FieldLabel>
            <Input
              id="preview-user"
              disabled={preview.isPending}
              value={userId}
              onChange={(event) => {
                setUserId(event.target.value);
                preview.reset();
              }}
              placeholder="Check a specific customer"
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="preview-model">Public model</FieldLabel>
            <Select
              disabled={preview.isPending}
              items={pool.models.map((value) => ({ value, label: value }))}
              value={model}
              onValueChange={(value) => {
                setModel(value);
                preview.reset();
              }}
            >
              <SelectTrigger id="preview-model" className="w-full">
                <SelectValue placeholder="Choose a public model" />
              </SelectTrigger>
              <SelectContent>
                {pool.models.map((value) => (
                  <SelectItem key={value} value={value}>
                    {value}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={preview.isPending}
            variant={mode === "fixed" ? "secondary" : "outline"}
            size="sm"
            onClick={() => {
              setMode("fixed");
              preview.reset();
            }}
          >
            Fixed: {pool.name}
          </Button>
          <Button
            disabled={preview.isPending}
            variant={mode === "auto" ? "secondary" : "outline"}
            size="sm"
            onClick={() => {
              setMode("auto");
              preview.reset();
            }}
          >
            Automatic eligible pool
          </Button>
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          Automatic routing chooses the highest-priority eligible pool, with lower numbers first. Weights apply within a
          deployment priority. Retries remain inside the selected pool; this preview does not contact an upstream
          provider.
        </p>
        {preview.error && (
          <Alert variant="error">
            <AlertTitle>No eligible route</AlertTitle>
            <AlertDescription>{preview.error.message}</AlertDescription>
          </Alert>
        )}
        {preview.data && (
          <div className="rounded-xl border bg-muted/20 p-5">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="size-4 text-success" />
              <h3 className="text-sm font-medium">Eligible route found</h3>
            </div>
            <p className="mt-3 text-lg font-semibold">{preview.data.pool_name}</p>
            <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{preview.data.pool_id}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Badge variant="outline">
                {preview.data.eligible_count ?? preview.data.allowed_deployment_ids.length} eligible deployments
              </Badge>
              <Badge variant="outline">Settings version {preview.data.settings_version}</Badge>
            </div>
            {preview.data.discount && (
              <div className="mt-4 rounded-lg border bg-background p-3">
                <p className="text-xs text-muted-foreground">Effective negotiated pricing for this route</p>
                <p className="mt-1 text-sm font-medium">{discountDescription(preview.data.discount)}</p>
                {preview.data.discount_source && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    {modelDiscountSourceLabel(preview.data.discount_source)}
                  </p>
                )}
                {preview.data.reference_prices && (
                  <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-muted-foreground">
                    {[
                      { key: "input_per_million" as const, label: "Input reference / 1M" },
                      { key: "output_per_million" as const, label: "Output reference / 1M" },
                      { key: "cache_read_per_million" as const, label: "Cache read reference / 1M" },
                    ].map((item) => (
                      <div key={item.key}>
                        {item.label}
                        <span className="mt-1 block text-foreground">
                          {formatMoney(preview.data?.reference_prices?.[item.key] ?? "")}
                        </span>
                      </div>
                    ))}
                    {cacheWritePriceRows(
                      preview.data.reference_prices.cache_write_per_million,
                      preview.data.reference_prices.cache_write_tiers,
                    ).map(({ key, label, amount }) => (
                      <div key={key}>
                        {label} / 1M
                        <span className="mt-1 block text-foreground">{formatMoney(amount ?? "")}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="mt-4 space-y-2">
              {preview.data.bindings.map((binding) => (
                <div
                  key={`${binding.deployment_id}:${binding.model}`}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-background px-3 py-2 text-xs"
                >
                  <span className="min-w-0 break-all font-mono">{binding.deployment_id}</span>
                  <span className="shrink-0 text-muted-foreground">
                    Priority {binding.priority} · Weight {binding.weight}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={preview.isPending} onClick={onClose}>
            Done
          </Button>
          <Button disabled={!model || preview.isPending} onClick={() => preview.mutate()}>
            <Route />
            {preview.isPending ? "Checking route..." : "Preview route"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
