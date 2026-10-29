"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, CircleAlert, Globe2, LockKeyhole } from "lucide-react";
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
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/lib/toast";
import { discountDescription, discountToPercent, percentToDiscount, retailRequest } from "../api";
import { PoolDeployment, poolIdFromName, ResourcePool, sourceKindLabel, sourceKinds } from "./types";
import PoolDeployments from "./PoolDeployments";
import type { BindingDraft } from "./deploymentGroups";

export interface PoolSeed {
  name: string;
  source_kind: string;
}

export default function PoolWizard({
  accessToken,
  pool,
  seed,
  onClose,
}: {
  accessToken: string | null;
  pool?: ResourcePool;
  seed?: PoolSeed;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [step, setStep] = useState(0);
  const [identity, setIdentity] = useState({
    id: pool?.id ?? "",
    name: pool?.name ?? seed?.name ?? "",
    source_kind: pool?.source_kind ?? seed?.source_kind ?? "",
    description: pool?.description ?? "",
    priority: String(pool?.priority ?? 0),
  });
  const [bindings, setBindings] = useState<BindingDraft[]>(
    pool?.bindings.map((binding) => ({
      ...binding,
      weight: String(binding.weight),
      priority: String(binding.priority),
    })) ?? [],
  );
  const [visibility, setVisibility] = useState<"public" | "restricted">(pool?.visibility ?? "restricted");
  const [percentage, setPercentage] = useState(discountToPercent(pool?.default_discount ?? "1"));
  const [search, setSearch] = useState("");
  const [validation, setValidation] = useState<string | null>(null);
  const deployments = useQuery({
    queryKey: ["retail", "pool-deployments"],
    queryFn: () => retailRequest<{ items: PoolDeployment[] }>("/admin/pools/deployments", accessToken),
    enabled: Boolean(accessToken),
  });
  const resolvedId = identity.id || poolIdFromName(identity.name);
  const price = percentToDiscount(percentage);
  const save = useMutation({
    mutationFn: (publish: boolean) =>
      retailRequest<ResourcePool>(pool ? `/admin/pools/${encodeURIComponent(pool.id)}` : "/admin/pools", accessToken, {
        method: pool ? "PUT" : "POST",
        body: JSON.stringify({
          ...identity,
          id: resolvedId,
          priority: Number(identity.priority),
          enabled: publish,
          visibility,
          default_discount: price,
          bindings: bindings.map((binding) => ({
            ...binding,
            weight: Number(binding.weight),
            priority: Number(binding.priority),
          })),
        }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      toast.success("Resource pool saved");
      onClose();
    },
  });
  const validInteger = (value: string, minimum: number) =>
    /^\d{1,7}$/.test(value) && Number(value) >= minimum && Number(value) <= 1_000_000;
  const validateStep = (target: number) => {
    if (
      target === 0 &&
      (!identity.name.trim() ||
        !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(resolvedId) ||
        !sourceKinds.some((item) => item.value === identity.source_kind) ||
        !validInteger(identity.priority, 0))
    )
      return "Enter a pool name, valid stable ID, explicit resource source, and a non-negative priority.";
    if (
      target === 1 &&
      bindings.some((binding) => !validInteger(binding.weight, 1) || !validInteger(binding.priority, 0))
    )
      return "Weights must be positive integers; priorities must be non-negative integers.";
    if (target === 2 && price === null)
      return "Enter a reference-price percentage greater than 0 and at most 100, with up to 7 decimal places.";
    return null;
  };
  const next = () => {
    const error = validateStep(step);
    setValidation(error);
    if (!error) setStep(step + 1);
  };
  const submit = (publish: boolean) => {
    if (publish && !bindings.length) {
      setValidation("Add at least one upstream deployment before publishing this pool.");
      return;
    }
    const error = [0, 1, 2].map(validateStep).find(Boolean);
    if (error) {
      setValidation(error);
      return;
    }
    setValidation(null);
    save.mutate(publish);
  };
  const saveLabel = pool?.enabled ? "Save published pool" : "Save draft";
  const models = [...new Set(bindings.map((item) => item.model))];
  const steps = ["Identity & source", "Channels & models", "Access & pricing", "Review"];
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) onClose();
      }}
    >
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>{pool ? "Edit resource pool" : "Create resource pool"}</DialogTitle>
          <DialogDescription>
            A guided setup. Sources, access, and pricing are saved together after review.
          </DialogDescription>
        </DialogHeader>
        <ol className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {steps.map((label, index) => (
            <li
              key={label}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-xs ${step === index ? "border-foreground/25 bg-muted font-medium" : "text-muted-foreground"}`}
              aria-current={step === index ? "step" : undefined}
            >
              {index < step ? (
                <Check className="size-4" />
              ) : (
                <span className="flex size-5 items-center justify-center rounded-full border text-[10px]">
                  {index + 1}
                </span>
              )}
              {label}
            </li>
          ))}
        </ol>
        {step === 0 && (
          <div className="grid gap-5 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="pool-name">Pool name</FieldLabel>
              <Input
                id="pool-name"
                value={identity.name}
                onChange={(event) => setIdentity({ ...identity, name: event.target.value })}
                placeholder="AWS Bedrock Claude"
                maxLength={100}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="pool-id">Stable pool ID</FieldLabel>
              <Input
                id="pool-id"
                value={identity.id}
                disabled={Boolean(pool)}
                onChange={(event) => setIdentity({ ...identity, id: event.target.value })}
                placeholder={poolIdFromName(identity.name) || "aws-bedrock-claude"}
                maxLength={80}
              />
              <p className="text-xs text-muted-foreground">
                Generated from the name when empty. Used in API key policies and invoices.
              </p>
            </Field>
            <Field>
              <FieldLabel htmlFor="pool-source">Resource source</FieldLabel>
              <Select
                items={sourceKinds}
                value={identity.source_kind || null}
                onValueChange={(value) => {
                  if (value) setIdentity({ ...identity, source_kind: value });
                }}
              >
                <SelectTrigger id="pool-source" className="w-full">
                  <SelectValue placeholder="Choose the actual resource source" />
                </SelectTrigger>
                <SelectContent>
                  {sourceKinds.map((source) => (
                    <SelectItem key={source.value} value={source.value}>
                      {source.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor="pool-priority">Pool priority</FieldLabel>
              <Input
                id="pool-priority"
                inputMode="numeric"
                value={identity.priority}
                onChange={(event) => setIdentity({ ...identity, priority: event.target.value })}
              />
              <p className="text-xs text-muted-foreground">
                Lower numbers are considered first between eligible pools.
              </p>
            </Field>
            <Field className="sm:col-span-2">
              <FieldLabel htmlFor="pool-description">Description</FieldLabel>
              <Textarea
                id="pool-description"
                value={identity.description}
                onChange={(event) => setIdentity({ ...identity, description: event.target.value })}
                maxLength={1000}
                placeholder="Describe this resource pool and its intended use"
              />
            </Field>
            <div className="rounded-lg border bg-muted/25 p-4 text-xs leading-relaxed text-muted-foreground sm:col-span-2">
              <p className="font-medium text-foreground">Resource source and API protocol are separate</p>
              <p className="mt-1">
                An Anthropic-compatible HTTP endpoint may serve resources from different sources. Choose the source
                based on your verified upstream arrangement. The gateway does not infer or verify provenance from an
                endpoint URL.
              </p>
            </div>
          </div>
        )}
        {step === 1 && (
          <PoolDeployments
            poolId={resolvedId}
            items={deployments.data?.items ?? []}
            loading={deployments.isPending}
            error={deployments.error}
            bindings={bindings}
            onChange={setBindings}
          />
        )}
        {step === 2 && (
          <div className="flex flex-col gap-5">
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                {
                  value: "restricted" as const,
                  title: "Restricted pool",
                  description: "Only explicit account grants can use this pool.",
                  icon: LockKeyhole,
                },
                {
                  value: "public" as const,
                  title: "Public pool",
                  description: "Accounts inheriting public pools can use its published models.",
                  icon: Globe2,
                },
              ].map((item) => (
                <button
                  type="button"
                  key={item.value}
                  aria-label={item.title}
                  onClick={() => setVisibility(item.value)}
                  aria-pressed={visibility === item.value}
                  className={`rounded-xl border p-4 text-left ${visibility === item.value ? "border-foreground/30 bg-muted/40" : "hover:bg-muted/20"}`}
                >
                  <item.icon className="mb-3 size-5 text-muted-foreground" />
                  <p className="text-sm font-medium">{item.title}</p>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{item.description}</p>
                </button>
              ))}
            </div>
            <Field className="max-w-sm">
              <FieldLabel htmlFor="pool-discount">Default percentage of reference price (%)</FieldLabel>
              <Input
                id="pool-discount"
                inputMode="decimal"
                value={percentage}
                onChange={(event) => setPercentage(event.target.value)}
              />
              <p className="text-xs leading-relaxed text-muted-foreground">
                70 means pay 70% of each model&apos;s reference price. Customer and shared-plan rules may override this
                default for this pool.
              </p>
            </Field>
            <p className="rounded-lg border bg-muted/20 p-4 text-xs leading-relaxed text-muted-foreground">
              A discount grants no access. Access is checked against explicit account pool-model grants and the API key
              policy before pricing is applied. Legacy model-only discounts are not copied into this pool.
            </p>
          </div>
        )}
        {step === 3 && (
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="rounded-xl border p-5">
              <p className="text-xs text-muted-foreground">Pool identity</p>
              <h3 className="mt-1 text-lg font-semibold">{identity.name}</h3>
              <p className="mt-1 break-all font-mono text-xs text-muted-foreground">{resolvedId}</p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Badge variant="outline">{sourceKindLabel(identity.source_kind)}</Badge>
                <Badge variant="outline">{visibility === "public" ? "Public" : "Restricted"}</Badge>
                <Badge variant="outline">Priority {identity.priority}</Badge>
              </div>
              <p className="mt-4 text-sm text-muted-foreground">{identity.description || "No description"}</p>
            </div>
            <div className="rounded-xl border p-5">
              <p className="text-xs text-muted-foreground">Routing and default pricing</p>
              <p className="mt-2 text-sm">
                {bindings.length} deployments · {models.length} public models
              </p>
              <p className="mt-3 text-sm font-medium">{price ? discountDescription(price) : "Invalid discount"}</p>
              <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                Bindings, source, visibility, and pricing are saved atomically. Saving a draft keeps it unavailable to
                customer requests. Publishing makes this pool available to explicitly configured resource-routing keys.
                Site-wide activation also applies pool routing to legacy keys.
              </p>
            </div>
            <div className="rounded-lg border p-4 sm:col-span-2">
              <p className="mb-2 text-xs text-muted-foreground">Public models in this pool</p>
              <div className="flex flex-wrap gap-2">
                {models.map((model) => (
                  <Badge variant="secondary" key={model}>
                    {model}
                  </Badge>
                ))}
              </div>
            </div>
          </div>
        )}
        {(validation || save.error) && (
          <Alert variant="error">
            <CircleAlert />
            <AlertTitle>Pool was not saved</AlertTitle>
            <AlertDescription>{validation ?? save.error?.message}</AlertDescription>
          </Alert>
        )}
        <DialogFooter className="flex-wrap">
          <Button variant="outline" disabled={save.isPending} onClick={onClose}>
            Cancel
          </Button>
          {step > 0 && (
            <Button
              variant="ghost"
              disabled={save.isPending}
              onClick={() => {
                setValidation(null);
                setStep(step - 1);
              }}
            >
              <ArrowLeft />
              Back
            </Button>
          )}
          {step < 3 ? (
            <Button onClick={next}>
              Continue
              <ArrowRight />
            </Button>
          ) : (
            <>
              <Button variant="outline" disabled={save.isPending} onClick={() => submit(pool?.enabled ?? false)}>
                {save.isPending ? "Saving..." : saveLabel}
              </Button>
              {!pool?.enabled && (
                <Button disabled={save.isPending} onClick={() => submit(true)}>
                  Save and publish
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
