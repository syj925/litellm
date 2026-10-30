"use client";

import { ChevronDown, Layers, LockKeyhole } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Alert, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { uiHref } from "@/utils/uiHref";
import {
  addDeploymentBindings,
  type BindingDraft,
  deploymentGroups,
  hasBinding,
  matchingDeploymentGroups,
} from "./deploymentGroups";
import type { PoolDeployment } from "./types";

export default function PoolDeployments({
  poolId,
  items,
  loading,
  error,
  bindings,
  onChange,
}: {
  poolId: string;
  items: PoolDeployment[];
  loading: boolean;
  error: Error | null;
  bindings: BindingDraft[];
  onChange: (bindings: BindingDraft[]) => void;
}) {
  const [search, setSearch] = useState("");
  const [showLegacy, setShowLegacy] = useState(false);
  const groups = matchingDeploymentGroups(deploymentGroups(items), search);
  const channels = groups.filter((group) => group.channelId !== null);
  const legacy = groups.filter((group) => group.channelId === null);
  const channelDeploymentIds = new Set(items.filter((item) => item.channel_id).map((item) => item.deployment_id));
  const currentChannels = channels.filter((group) => group.poolId === poolId);
  const editBinding = (index: number, field: "weight" | "priority", value: string) =>
    onChange(bindings.map((binding, current) => (current === index ? { ...binding, [field]: value } : binding)));
  const toggleLegacy = (item: PoolDeployment, checked: boolean) =>
    onChange(
      checked
        ? addDeploymentBindings(bindings, [item])
        : bindings.filter((binding) => binding.deployment_id !== item.deployment_id || binding.model !== item.model),
    );
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border bg-muted/20 p-4">
        <div className="max-w-2xl text-sm">
          <p className="font-medium">Assign named channels when you add an upstream</p>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            Each channel has a name and one resource pool. All models added through that channel appear together here,
            even when their public model names match another channel. Create this pool first, as a draft or published
            pool, then choose it when adding a channel.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          nativeButton={false}
          role="link"
          render={<Link href={uiHref("channels")} target="_blank" rel="noopener noreferrer" />}
        >
          Manage channels
        </Button>
      </div>
      <Input
        aria-label="Search upstream deployments"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="Search channel name, model, or deployment ID..."
      />
      <p className="text-xs text-muted-foreground">
        {currentChannels.length} channels in this pool · {bindings.length} deployment bindings selected. Channel
        membership is managed in Channels. Routing weights and priorities can be adjusted below.
      </p>
      {loading && (
        <LoadingState title="Loading channels" description="Retrieving named channels and their upstream models." />
      )}
      {!loading && error && (
        <Alert variant="error">
          <AlertTitle>{error.message}</AlertTitle>
        </Alert>
      )}
      {!loading && !error && (
        <>
          <div className="max-h-80 space-y-2 overflow-y-auto">
            {channels.map((group) => {
              const belongsHere = group.poolId === poolId;
              const selected = group.items.filter((item) => hasBinding(bindings, item)).length;
              const models = [...new Set(group.items.map((item) => item.model))];
              return (
                <Collapsible key={group.key} role="group" aria-label={group.name} className="rounded-lg border">
                  <div className="flex items-start gap-3 p-4">
                    <Checkbox
                      aria-label={`Channel ${group.name}`}
                      className="mt-1"
                      checked={selected === group.items.length}
                      indeterminate={selected > 0 && selected < group.items.length}
                      disabled={!belongsHere || selected === group.items.length}
                      onCheckedChange={(checked) => {
                        if (checked) onChange(addDeploymentBindings(bindings, group.items));
                      }}
                    />
                    <div className="min-w-0 flex-1">
                      <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                        <Layers className="size-4 text-muted-foreground" />
                        {group.name}
                        <Badge variant={belongsHere ? "secondary" : "outline"}>
                          {belongsHere ? "This pool" : group.poolName || group.poolId}
                        </Badge>
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {models.length} models · {group.items.length} deployments · {group.items[0].provider}
                        {group.items[0].endpoint_host ? ` · ${group.items[0].endpoint_host}` : ""}
                      </p>
                      {!belongsHere && (
                        <p className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
                          <LockKeyhole className="size-3" />
                          Move this channel in Channels to change its pool.
                        </p>
                      )}
                    </div>
                    <CollapsibleTrigger render={<Button variant="ghost" size="xs" />}>
                      View models
                      <ChevronDown className="size-3" />
                    </CollapsibleTrigger>
                  </div>
                  <CollapsibleContent>
                    <div className="space-y-2 border-t bg-muted/15 px-4 py-3">
                      {group.items.map((item) => (
                        <div
                          key={`${item.deployment_id}:${item.model}`}
                          className="flex flex-wrap items-center justify-between gap-2 text-xs"
                        >
                          <span>{item.model}</span>
                          <span className="font-mono text-[10px] text-muted-foreground" title={item.deployment_id}>
                            {item.deployment_id.slice(0, 12)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </CollapsibleContent>
                </Collapsible>
              );
            })}
            {channels.length === 0 && (
              <div className="rounded-lg border border-dashed p-5 text-center text-sm text-muted-foreground">
                {search
                  ? "No named channels match your search."
                  : "No named channels yet. Add a channel and choose its resource pool in Models + Endpoints."}
              </div>
            )}
          </div>
          {legacy.length > 0 && (
            <Collapsible open={showLegacy} onOpenChange={setShowLegacy} className="rounded-lg border">
              <CollapsibleTrigger className="flex w-full items-center justify-between gap-3 p-4 text-left">
                <span>
                  <span className="block text-sm font-medium">Existing unnamed deployments ({legacy.length})</span>
                  <span className="mt-1 block text-xs text-muted-foreground">
                    Name and group these in Channels, or review each deployment individually here.
                  </span>
                </span>
                <ChevronDown className="size-4 shrink-0" />
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div className="border-t p-3">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <p className="text-xs text-muted-foreground">Model names do not identify their upstream source.</p>
                    <Button
                      variant="outline"
                      size="xs"
                      onClick={() =>
                        onChange(
                          addDeploymentBindings(
                            bindings,
                            legacy.flatMap((group) => group.items),
                          ),
                        )
                      }
                    >
                      Select visible unnamed deployments
                    </Button>
                  </div>
                  <div className="max-h-56 overflow-auto">
                    {legacy.map((group) =>
                      group.items.map((item) => (
                        <div
                          key={`${item.deployment_id}:${item.model}`}
                          className="flex items-center gap-3 border-t px-2 py-3 first:border-t-0"
                        >
                          <Checkbox
                            aria-label={`Bind ${item.model} deployment ${item.deployment_id}`}
                            checked={hasBinding(bindings, item)}
                            onCheckedChange={(checked) => toggleLegacy(item, checked === true)}
                          />
                          <span className="min-w-0 flex-1 text-xs">
                            <span className="block font-medium">{item.model}</span>
                            <span className="mt-1 block text-muted-foreground">
                              {item.provider} · {item.credential_name || "No saved credential name"}
                              {item.endpoint_host ? ` · ${item.endpoint_host}` : ""}
                            </span>
                          </span>
                          <span
                            className="max-w-48 break-all font-mono text-[10px] text-muted-foreground"
                            title={item.deployment_id}
                          >
                            {item.deployment_id.slice(0, 12)}
                          </span>
                        </div>
                      )),
                    )}
                  </div>
                </div>
              </CollapsibleContent>
            </Collapsible>
          )}
        </>
      )}
      {bindings.length > 0 && (
        <Collapsible className="rounded-lg border">
          <CollapsibleTrigger className="flex w-full items-center justify-between gap-3 p-4 text-sm font-medium">
            Advanced routing weights and priorities
            <ChevronDown className="size-4" />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <p className="px-4 pb-3 text-xs text-muted-foreground">
              Lower priorities run first. Weights balance deployments at the same priority.
            </p>
            <div className="max-h-64 overflow-auto border-t">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Channel / Model</TableHead>
                    <TableHead>Weight</TableHead>
                    <TableHead>Priority</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {bindings.map((binding, index) => {
                    const deployment = items.find(
                      (item) => item.deployment_id === binding.deployment_id && item.model === binding.model,
                    );
                    return (
                      <TableRow key={`${binding.deployment_id}:${binding.model}`}>
                        <TableCell className="max-w-72 whitespace-normal">
                          <p className="text-xs font-medium">{deployment?.channel_name || "Unnamed deployment"}</p>
                          <p className="mt-1 text-xs">{binding.model}</p>
                          <p className="mt-1 font-mono text-[10px] text-muted-foreground" title={binding.deployment_id}>
                            {binding.deployment_id.slice(0, 12)}
                          </p>
                        </TableCell>
                        <TableCell>
                          <Input
                            aria-label={`Weight for ${binding.model} ${binding.deployment_id}`}
                            className="w-20"
                            inputMode="numeric"
                            value={binding.weight}
                            onChange={(event) => editBinding(index, "weight", event.target.value)}
                          />
                        </TableCell>
                        <TableCell>
                          <Input
                            aria-label={`Priority for ${binding.model} ${binding.deployment_id}`}
                            className="w-20"
                            inputMode="numeric"
                            value={binding.priority}
                            onChange={(event) => editBinding(index, "priority", event.target.value)}
                          />
                        </TableCell>
                        <TableCell>
                          {channelDeploymentIds.has(binding.deployment_id) ? (
                            <span className="text-xs text-muted-foreground">Assigned via channel</span>
                          ) : (
                            <Button
                              variant="ghost"
                              size="xs"
                              onClick={() => onChange(bindings.filter((_, current) => current !== index))}
                            >
                              Remove
                            </Button>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}
