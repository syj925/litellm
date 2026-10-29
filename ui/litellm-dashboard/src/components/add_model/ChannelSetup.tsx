"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useFormContext } from "react-hook-form";
import { Plus } from "lucide-react";
import { MountedFormField, type MountedFormValues } from "../common_components/MountedFormField";
import { retailRequest, RetailRequestError } from "../retail/api";
import type { ChannelCatalog } from "../retail/channels/types";
import { type ResourcePool, sourceKinds, sourceKindLabel } from "../retail/pools/types";
import { SearchSelect } from "../shared/SearchSelect";
import { Alert, AlertDescription, AlertTitle } from "../shared/Alert";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { RadioGroup, RadioGroupItem } from "../ui/radio-group";
import { Field, FieldLabel } from "../ui/field";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { uiHref } from "@/utils/uiHref";
import { channelSelectionError, parseChannelSelection } from "./channelSelection";

export default function ChannelSetup({ accessToken }: { accessToken: string | null }) {
  const client = useQueryClient();
  const form = useFormContext<MountedFormValues>();
  const [creatingPool, setCreatingPool] = useState(false);
  const [poolName, setPoolName] = useState("");
  const [poolSource, setPoolSource] = useState("custom");
  const catalog = useQuery({
    queryKey: ["retail", "channels"],
    queryFn: async () => {
      try {
        return await retailRequest<ChannelCatalog>("/admin/channels", accessToken);
      } catch (error) {
        if (error instanceof RetailRequestError && error.status === 404) return { enabled: false, items: [] };
        throw error;
      }
    },
    enabled: Boolean(accessToken),
    retry: false,
  });
  const pools = useQuery({
    queryKey: ["retail", "pools", "admin"],
    queryFn: () => retailRequest<{ items: ResourcePool[] }>("/admin/pools", accessToken),
    enabled: catalog.data?.enabled === true,
    retry: false,
  });
  const createPool = useMutation({
    mutationFn: () =>
      retailRequest<ResourcePool>("/admin/pools", accessToken, {
        method: "POST",
        body: JSON.stringify({
          id: crypto.randomUUID(),
          name: poolName.trim(),
          source_kind: poolSource,
          description: "",
          enabled: false,
          visibility: "restricted",
          priority: 100,
          default_discount: "1",
          bindings: [],
        }),
      }),
    onSuccess: (pool) => {
      void client.invalidateQueries({ queryKey: ["retail", "pools", "admin"] });
      const current = parseChannelSelection(form.getValues("retail_channel"));
      if (current?.mode === "new") form.setValue("retail_channel", { ...current, pool_id: pool.id });
      setCreatingPool(false);
      setPoolName("");
    },
  });
  if (catalog.data?.enabled === false) return null;
  const channels = catalog.data?.items ?? [];
  const poolItems = pools.data?.items ?? [];
  const loading = catalog.isPending || (!catalog.error && pools.isPending);
  const failed = catalog.error ?? pools.error;
  return (
    <MountedFormField
      name="retail_channel"
      defaultValue={{ mode: "new", name: "", pool_id: "" }}
      rules={{
        validate: (value) =>
          loading
            ? "Wait for channel configuration to load"
            : failed
              ? "Channel configuration is unavailable. Retry before saving"
              : channelSelectionError(value, channels, poolItems) ?? true,
      }}
      className="mb-6"
    >
      {(control) => {
        const selection = parseChannelSelection(control.value) ?? { mode: "new" as const, name: "", pool_id: "" };
        const existing =
          selection.mode === "existing" ? channels.find((item) => item.id === selection.channel_id) : undefined;
        const selectedPool = poolItems.find(
          (pool) => pool.id === (selection.mode === "new" ? selection.pool_id : existing?.pool_id),
        );
        return (
          <section className="rounded-lg border bg-muted/20 p-4" aria-label="Channel and resource pool">
            <h3 className="font-medium">Channel and resource pool</h3>
            <p className="mt-1 mb-4 text-sm text-muted-foreground">
              Name this upstream connection once. All models added below join the same channel and resource pool.
            </p>
            {loading ? (
              <p role="status" className="text-sm text-muted-foreground">
                Loading channels and resource pools...
              </p>
            ) : failed ? (
              <Alert variant="destructive">
                <AlertTitle>Unable to load channel configuration</AlertTitle>
                <AlertDescription>
                  {failed.message}
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="ml-3"
                    onClick={() => {
                      void catalog.refetch();
                      void pools.refetch();
                    }}
                  >
                    Retry
                  </Button>
                </AlertDescription>
              </Alert>
            ) : (
              <>
                <RadioGroup
                  value={selection.mode}
                  className="mb-4 flex flex-wrap gap-5"
                  onValueChange={(mode) =>
                    control.onChange(
                      mode === "existing" ? { mode, channel_id: "" } : { mode: "new", name: "", pool_id: "" },
                    )
                  }
                >
                  <label className="flex items-center gap-2 text-sm">
                    <RadioGroupItem value="new" />
                    New channel
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <RadioGroupItem
                      value="existing"
                      disabled={channels.every((channel) => channel.status !== "active")}
                    />
                    Add models to existing channel
                  </label>
                </RadioGroup>
                {selection.mode === "new" ? (
                  <div className="grid gap-4 md:grid-cols-2">
                    <Field>
                      <FieldLabel htmlFor={`${control.id}-name`}>Channel name</FieldLabel>
                      <Input
                        id={`${control.id}-name`}
                        value={selection.name}
                        maxLength={100}
                        placeholder="e.g. Bedrock supplier A"
                        onChange={(event) => control.onChange({ ...selection, name: event.target.value })}
                      />
                    </Field>
                    <Field>
                      <div className="flex items-center justify-between gap-3">
                        <FieldLabel htmlFor={`${control.id}-pool`}>Resource pool</FieldLabel>
                        <Button
                          type="button"
                          size="xs"
                          variant="ghost"
                          onClick={() => {
                            createPool.reset();
                            setCreatingPool(true);
                          }}
                        >
                          <Plus className="size-3" />
                          Create pool
                        </Button>
                      </div>
                      <SearchSelect
                        inputId={`${control.id}-pool`}
                        value={selection.pool_id || null}
                        onValueChange={(value) => control.onChange({ ...selection, pool_id: value ?? "" })}
                        options={poolItems.map((pool) => ({
                          value: pool.id,
                          label: `${pool.name} · ${pool.enabled ? "Published" : "Draft"}`,
                        }))}
                        placeholder="Select a resource pool"
                        emptyText="Create your first resource pool"
                      />
                    </Field>
                  </div>
                ) : (
                  <Field>
                    <FieldLabel htmlFor={`${control.id}-existing`}>Existing channel</FieldLabel>
                    <SearchSelect
                      inputId={`${control.id}-existing`}
                      value={selection.channel_id || null}
                      onValueChange={(value) => control.onChange({ mode: "existing", channel_id: value ?? "" })}
                      options={channels
                        .filter((channel) => channel.status === "active")
                        .map((channel) => ({
                          value: channel.id,
                          label: `${channel.name} · ${channel.pool_name} · ${channel.deployment_count} deployments`,
                        }))}
                      placeholder="Select a channel"
                    />
                    {existing && (
                      <p className="text-sm text-muted-foreground">
                        Resource pool: {existing.pool_name}. New deployments use the credentials entered below.
                      </p>
                    )}
                  </Field>
                )}
                {selectedPool && (
                  <p className="mt-3 text-sm text-muted-foreground">
                    {sourceKindLabel(selectedPool.source_kind)} ·{" "}
                    {selectedPool.enabled
                      ? "Published pool: models become available to its authorized keys."
                      : "Draft pool: publish it after reviewing access and prices to serve resource-routing keys."}
                  </p>
                )}
                <p className="mt-3 text-sm text-muted-foreground">
                  The Provider field below selects the HTTP integration. Resource source is configured separately.{" "}
                  <a className="text-primary hover:underline" href={uiHref("/resource-pools/")}>
                    Pool setup guide
                  </a>
                </p>
              </>
            )}
            <Dialog open={creatingPool} onOpenChange={setCreatingPool}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Create resource pool</DialogTitle>
                  <DialogDescription>
                    Create a restricted draft, then add this channel. Publish later from Resource Pools after reviewing
                    access and discounts.
                  </DialogDescription>
                </DialogHeader>
                <Field>
                  <FieldLabel htmlFor="new-channel-pool-name">Pool name</FieldLabel>
                  <Input
                    id="new-channel-pool-name"
                    value={poolName}
                    maxLength={100}
                    onChange={(event) => setPoolName(event.target.value)}
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="new-channel-pool-source">Resource source</FieldLabel>
                  <Select value={poolSource} onValueChange={(value) => setPoolSource(value ?? "custom")}>
                    <SelectTrigger id="new-channel-pool-source">
                      <SelectValue />
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
                {createPool.error && (
                  <p role="alert" className="text-sm text-destructive">
                    {createPool.error.message}
                  </p>
                )}
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={() => setCreatingPool(false)}>
                    Cancel
                  </Button>
                  <Button
                    type="button"
                    disabled={!poolName.trim() || createPool.isPending}
                    onClick={() => createPool.mutate()}
                  >
                    {createPool.isPending ? "Creating..." : "Create draft"}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </section>
        );
      }}
    </MountedFormField>
  );
}
