"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
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
import { Input } from "@/components/ui/input";
import { toast } from "@/lib/toast";
import { retailRequest } from "../api";
import type { ResourcePool } from "../pools/types";
import { ChannelDeployment, groupConnections, toggleDeployments } from "./connections";
import type { NamedChannel } from "./types";

export default function ChannelEditor({
  channel,
  initialIds = [],
  deployments,
  pools,
  accessToken,
  onClose,
}: {
  channel?: NamedChannel;
  initialIds?: string[];
  deployments: ChannelDeployment[];
  pools: ResourcePool[];
  accessToken: string | null;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(channel?.name || "");
  const [poolId, setPoolId] = useState(channel?.pool_id || "");
  const [selected, setSelected] = useState(channel?.deployment_ids || initialIds);
  const [search, setSearch] = useState("");
  const eligible = deployments.filter((item) => !item.channel_id || item.channel_id === channel?.id);
  const liveIds = new Set(deployments.map((item) => item.deployment_id));
  const missingIds = selected.filter((id) => !liveIds.has(id));
  const visible = eligible.filter((item) =>
    `${item.model} ${item.provider} ${item.endpoint_host || ""} ${item.credential_name || ""} ${item.deployment_id}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const save = useMutation({
    mutationFn: () =>
      retailRequest<NamedChannel>(
        channel ? `/admin/channels/${encodeURIComponent(channel.id)}` : "/admin/channels",
        accessToken,
        {
          method: channel ? "PUT" : "POST",
          body: JSON.stringify({
            name: name.trim(),
            pool_id: poolId,
            deployment_ids: selected,
            ...(channel ? { expected_version: channel.version } : {}),
          }),
        },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      toast.success("Channel and resource pool assignment saved");
      onClose();
    },
  });
  const pool = pools.find((item) => item.id === poolId);
  const moving = channel && channel.pool_id !== poolId;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) onClose();
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>{channel ? "Edit channel" : "Name existing deployments"}</DialogTitle>
          <DialogDescription>
            Give this upstream connection a recognizable name and assign its models to one resource pool.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="flex flex-col gap-2 text-sm font-medium">
            Channel name
            <Input
              value={name}
              maxLength={100}
              placeholder="e.g. AWS Claude primary"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="flex flex-col gap-2 text-sm font-medium">
            Resource pool
            <select
              className="h-9 rounded-md border bg-background px-3 font-normal"
              value={poolId}
              onChange={(event) => setPoolId(event.target.value)}
            >
              <option value="">Choose a resource pool</option>
              {pools.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                  {item.enabled ? "" : " (draft)"}
                </option>
              ))}
            </select>
          </label>
        </div>
        {pool && !pool.enabled && (
          <p className="text-sm text-muted-foreground">
            This pool is a draft. Review access and pricing, then publish it before serving pool-routed keys.
          </p>
        )}
        {moving && (
          <Alert>
            <AlertTitle>Move every model in this channel</AlertTitle>
            <AlertDescription>
              The old pool will stop serving these deployments. Existing keys keep their current pool permissions and
              prices; they are not moved with the channel.
            </AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-sm font-medium">{selected.length} deployments selected</span>
          <Input
            className="sm:w-80"
            aria-label="Search channel deployments"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search model, endpoint, credential or ID"
          />
        </div>
        <p className="text-xs text-muted-foreground">
          Connections are grouped by their actual endpoint and credential identity. Source labels are assigned by you.
          Public model names stay unchanged.
        </p>
        {missingIds.length > 0 && (
          <Alert>
            <AlertTitle>Deployments removed upstream</AlertTitle>
            <AlertDescription>
              These saved members no longer exist. Remove them from the channel before saving.
              <div className="mt-2 space-y-2">
                {missingIds.map((id) => (
                  <div key={id} className="flex items-center justify-between gap-2">
                    <code className="break-all text-xs">{id}</code>
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={`Remove missing deployment ${id}`}
                      onClick={() => setSelected(selected.filter((value) => value !== id))}
                    >
                      Remove
                    </Button>
                  </div>
                ))}
              </div>
            </AlertDescription>
          </Alert>
        )}
        <div className="max-h-80 space-y-3 overflow-y-auto rounded-lg border p-3">
          {groupConnections(visible).map((group) => {
            const ids = group.items.map((item) => item.deployment_id);
            const count = ids.filter((id) => selected.includes(id)).length;
            return (
              <section key={group.id} className="rounded-md border">
                <div className="flex items-center gap-3 bg-muted/40 p-3">
                  <Checkbox
                    aria-label={`Select connection ${group.label} ${group.id.slice(0, 8)}`}
                    checked={count === ids.length}
                    indeterminate={count > 0 && count < ids.length}
                    onCheckedChange={(checked) => setSelected(toggleDeployments(selected, ids, checked === true))}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{group.label}</p>
                    <p className="text-xs text-muted-foreground">
                      {group.items[0].provider} · Connection {group.id.slice(0, 8)} · {ids.length} deployments
                    </p>
                  </div>
                  <Badge variant="outline">{count} selected</Badge>
                </div>
                <div className="divide-y">
                  {group.items.map((item) => (
                    <label key={item.deployment_id} className="flex items-center gap-3 px-3 py-2 text-sm">
                      <Checkbox
                        aria-label={`Select ${item.model} ${item.deployment_id}`}
                        checked={selected.includes(item.deployment_id)}
                        onCheckedChange={(checked) =>
                          setSelected(toggleDeployments(selected, [item.deployment_id], checked === true))
                        }
                      />
                      <span className="flex-1">
                        {item.model}
                        <span className="ml-2 text-xs text-muted-foreground">{item.pool_name || "Unassigned"}</span>
                      </span>
                      <span className="font-mono text-xs text-muted-foreground">{item.deployment_id.slice(0, 8)}</span>
                    </label>
                  ))}
                </div>
              </section>
            );
          })}
          {visible.length === 0 && (
            <p className="p-6 text-center text-sm text-muted-foreground">
              No available deployments. Models already owned by another channel must be managed there.
            </p>
          )}
        </div>
        {save.error && (
          <Alert variant="destructive">
            <AlertTitle>Channel could not be saved</AlertTitle>
            <AlertDescription>{save.error.message}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={save.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={
              save.isPending || !name.trim() || !poolId || missingIds.length > 0 || (!channel && selected.length === 0)
            }
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save channel"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
