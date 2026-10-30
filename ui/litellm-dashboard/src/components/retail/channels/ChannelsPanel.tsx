"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Cable, Layers, Pencil, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/lib/toast";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { retailRequest } from "../api";
import type { ResourcePool } from "../pools/types";
import ChannelEditor from "./ChannelEditor";
import { ChannelDeployment, groupConnections } from "./connections";
import type { ChannelCatalog, NamedChannel } from "./types";

export default function ChannelsPanel() {
  const { accessToken, userRole, isAuthorized, isLoading } = useAuthorized();
  const isAdmin = isProxyAdminRole(userRole);
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<{ channel?: NamedChannel; ids?: string[] } | null>(null);
  const [recovering, setRecovering] = useState<NamedChannel | null>(null);
  const recovery = useMutation({
    mutationFn: (channel: NamedChannel) =>
      retailRequest(`/admin/channels/${encodeURIComponent(channel.id)}/recover`, accessToken, { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setRecovering(null);
      toast.success("Incomplete channel batch reconciled");
    },
  });
  const enabled = isAuthorized && isAdmin && Boolean(accessToken);
  const channels = useQuery({
    queryKey: ["retail", "channels"],
    queryFn: () => retailRequest<ChannelCatalog>("/admin/channels", accessToken),
    enabled,
  });
  const deployments = useQuery({
    queryKey: ["retail", "pool-deployments"],
    queryFn: () => retailRequest<{ items: ChannelDeployment[] }>("/admin/pools/deployments", accessToken),
    enabled,
  });
  const pools = useQuery({
    queryKey: ["retail", "pools", "admin"],
    queryFn: () => retailRequest<{ items: ResourcePool[] }>("/admin/pools", accessToken),
    enabled,
  });
  if (!isLoading && isAuthorized && !isAdmin)
    return (
      <StateMessage
        role="alert"
        icon={<ShieldCheck />}
        title="Administrator access required"
        description="Channels and upstream connections are managed by proxy administrators."
      />
    );
  const items = channels.data?.items || [];
  const allDeployments = deployments.data?.items || [];
  const unassigned = allDeployments.filter((item) => !item.channel_id);
  const matching = items.filter((item) =>
    `${item.name} ${item.pool_name} ${item.models.join(" ")}`.toLowerCase().includes(search.toLowerCase()),
  );
  const error = channels.error || deployments.error || pools.error;
  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Cable className="size-6" />
            Channels
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Named upstream connections, their models, and the resource pool they serve.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" render={<Link href={`${uiHref("usage")}?usage_tab=channels`} />}>
            <Activity />
            Channel activity
          </Button>
          <Button variant="outline" render={<Link href={uiHref("resource-pools")} />}>
            <Layers />
            Resource pools
          </Button>
          <Button render={<Link href={`${uiHref("models-and-endpoints")}?tab=add`} />}>
            <Plus />
            Add upstream models
          </Button>
        </div>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        {[
          { label: "Named channels", value: items.length },
          { label: "Channel deployments", value: allDeployments.length - unassigned.length },
          { label: "Awaiting channel name", value: unassigned.length },
        ].map((stat) => (
          <Card key={stat.label}>
            <CardContent className="py-5">
              <p className="text-sm text-muted-foreground">{stat.label}</p>
              <p className="mt-2 text-2xl font-semibold">{stat.value}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Alert>
        <AlertTitle>One channel, many models, one resource pool</AlertTitle>
        <AlertDescription>
          When adding models, name the channel and choose its pool once. The provider selects the HTTP protocol; the
          pool records the resource source. Customers continue to use the same public model names.
        </AlertDescription>
      </Alert>
      {error && (
        <Alert variant="destructive">
          <AlertTitle>Channels could not be loaded</AlertTitle>
          <AlertDescription>{error.message}</AlertDescription>
        </Alert>
      )}
      {(isLoading || channels.isLoading || deployments.isLoading || pools.isLoading) && (
        <LoadingState title="Loading channels…" description="Loading upstream connections and resource pools." />
      )}
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
          <CardTitle>Upstream channels</CardTitle>
          <div className="flex gap-2">
            <Input
              aria-label="Search channels"
              placeholder="Search channel, pool or model"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <Button
              variant="outline"
              size="icon"
              aria-label="Refresh channels"
              onClick={() => void client.invalidateQueries({ queryKey: ["retail"] })}
            >
              <RefreshCw />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Channel</TableHead>
                <TableHead>Resource pool</TableHead>
                <TableHead>Models</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {matching.map((item) => (
                <TableRow key={item.id}>
                  <TableCell className="font-medium">
                    <Link
                      href={`${uiHref("channel-details")}?channel=${encodeURIComponent(item.id)}`}
                      className="underline-offset-4 hover:underline"
                    >
                      {item.name}
                    </Link>
                    <p className="mt-1 font-mono text-xs font-normal text-muted-foreground">{item.id.slice(0, 8)}</p>
                  </TableCell>
                  <TableCell>{item.pool_name}</TableCell>
                  <TableCell>
                    <details>
                      <summary className="cursor-pointer text-sm">
                        {item.models.length} models · {item.deployment_count} deployments
                      </summary>
                      <div className="mt-2 flex max-w-md flex-wrap gap-1">
                        {item.models.map((model) => (
                          <Badge key={model} variant="outline">
                            {model}
                          </Badge>
                        ))}
                      </div>
                    </details>
                  </TableCell>
                  <TableCell>
                    <Badge variant={item.status === "error" ? "destructive" : "outline"}>
                      {item.status === "active" ? "Configured" : item.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      className="mr-2"
                      variant="outline"
                      size="sm"
                      render={<Link href={`${uiHref("channel-details")}?channel=${encodeURIComponent(item.id)}`} />}
                    >
                      <Activity />
                      View activity
                    </Button>
                    {item.status !== "active" && (
                      <Button
                        className="mr-2"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          recovery.reset();
                          setRecovering(item);
                        }}
                      >
                        Review recovery
                      </Button>
                    )}
                    <Button
                      variant="outline"
                      size="sm"
                      aria-label={`Edit channel ${item.name}`}
                      onClick={() => setEditing({ channel: item })}
                    >
                      <Pencil />
                      Edit
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {matching.length === 0 && (
                <TableRow>
                  <TableCell colSpan={5} className="py-10 text-center text-muted-foreground">
                    {items.length
                      ? "No matching channels."
                      : "Create a named channel when adding models, or organize your existing deployments below."}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      {unassigned.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Organize existing deployments</CardTitle>
            <p className="text-sm text-muted-foreground">
              Group by the actual connection, then give it a name and select a pool. Nothing is assigned automatically.
            </p>
          </CardHeader>
          <CardContent className="space-y-3">
            {groupConnections(unassigned).map((group) => (
              <div key={group.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4">
                <div>
                  <p className="font-medium">{group.label}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {group.items[0].provider} · Connection {group.id.slice(0, 8)} · {group.items.length} deployments
                  </p>
                  <p className="mt-1 max-w-xl truncate text-xs text-muted-foreground">
                    {group.items
                      .slice(0, 5)
                      .map((item) => item.model)
                      .join(", ")}
                    {group.items.length > 5 ? ", …" : ""}
                  </p>
                </div>
                <Button
                  variant="outline"
                  disabled={!pools.data?.items.length}
                  onClick={() => setEditing({ ids: group.items.map((item) => item.deployment_id) })}
                >
                  Name and assign
                </Button>
              </div>
            ))}
            {!pools.isLoading && !pools.data?.items.length && (
              <p className="text-sm text-muted-foreground">
                Create a resource pool first. An empty draft is enough to organize channels.
              </p>
            )}
          </CardContent>
        </Card>
      )}
      {editing && (
        <ChannelEditor
          channel={editing.channel}
          initialIds={editing.ids}
          deployments={allDeployments}
          pools={pools.data?.items || []}
          accessToken={accessToken}
          onClose={() => setEditing(null)}
        />
      )}
      {recovering && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open && !recovery.isPending) setRecovering(null);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Recover {recovering.name}</DialogTitle>
              <DialogDescription>
                Remove only the incomplete provisioning batch. Previously configured models stay in this channel. A
                creation operation that is still running cannot be recovered.
              </DialogDescription>
            </DialogHeader>
            <p className="text-sm">{recovering.pending_deployment_ids?.length || 0} pending deployments</p>
            <div className="max-h-48 overflow-auto rounded-md border p-3 font-mono text-xs">
              {recovering.pending_deployment_ids?.map((id) => <p key={id}>{id}</p>)}
            </div>
            {recovery.error && (
              <Alert variant="destructive">
                <AlertTitle>Recovery could not be completed</AlertTitle>
                <AlertDescription>{recovery.error.message}</AlertDescription>
              </Alert>
            )}
            <DialogFooter>
              <Button variant="outline" disabled={recovery.isPending} onClick={() => setRecovering(null)}>
                Cancel
              </Button>
              <Button disabled={recovery.isPending} onClick={() => recovery.mutate(recovering)}>
                {recovery.isPending ? "Recovering…" : "Recover incomplete batch"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
