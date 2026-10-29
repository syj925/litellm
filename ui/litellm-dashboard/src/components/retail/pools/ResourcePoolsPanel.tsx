"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  BookOpen,
  CheckCircle2,
  CircleAlert,
  Layers,
  Plus,
  RefreshCw,
  Route,
  Search,
  ShieldCheck,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
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
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "@/lib/toast";
import { isProxyAdminRole } from "@/utils/roles";
import { uiHref } from "@/utils/uiHref";
import { discountToPercent, retailRequest } from "../api";
import PoolGuide from "./PoolGuide";
import PoolPreview from "./PoolPreview";
import PoolWizard, { PoolSeed } from "./PoolWizard";
import { PoolSettings, poolTemplates, ResourcePool, sourceKindLabel } from "./types";

export default function ResourcePoolsPanel() {
  const { accessToken, userRole, isAuthorized, isLoading } = useAuthorized();
  const isAdmin = isProxyAdminRole(userRole);
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [showGuide, setShowGuide] = useState(false);
  const [editing, setEditing] = useState<{ pool?: ResourcePool; seed?: PoolSeed } | null>(null);
  const [preview, setPreview] = useState<ResourcePool | null>(null);
  const [activationOpen, setActivationOpen] = useState(false);
  const [activationReviewed, setActivationReviewed] = useState(false);
  const [disabling, setDisabling] = useState<ResourcePool | null>(null);
  const pools = useQuery({
    queryKey: ["retail", "pools", "admin"],
    queryFn: () => retailRequest<{ items: ResourcePool[] }>("/admin/pools", accessToken),
    enabled: isAuthorized && isAdmin && Boolean(accessToken),
  });
  const settings = useQuery({
    queryKey: ["retail", "pool-settings"],
    queryFn: () => retailRequest<PoolSettings>("/admin/pools/settings", accessToken),
    enabled: isAuthorized && isAdmin && Boolean(accessToken),
  });
  const activation = useMutation({
    mutationFn: (routing_enabled: boolean) =>
      retailRequest<PoolSettings>("/admin/pools/settings", accessToken, {
        method: "PUT",
        body: JSON.stringify({ routing_enabled }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setActivationOpen(false);
      toast.success("Resource pool routing settings saved");
    },
  });
  const disable = useMutation({
    mutationFn: (pool: ResourcePool) =>
      retailRequest(`/admin/pools/${encodeURIComponent(pool.id)}`, accessToken, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      setDisabling(null);
      toast.success("Resource pool unpublished");
    },
  });
  const items = pools.data?.items ?? [];
  const filtered = items.filter((pool) =>
    `${pool.name} ${pool.id} ${sourceKindLabel(pool.source_kind)}`.toLowerCase().includes(search.toLowerCase()),
  );
  const published = items.filter((pool) => pool.enabled);
  const openActivation = () => {
    setActivationReviewed(false);
    activation.reset();
    setActivationOpen(true);
  };
  if (!isLoading && isAuthorized && !isAdmin)
    return (
      <StateMessage
        role="alert"
        icon={<ShieldCheck />}
        title="Administrator access required"
        description="Resource pool sources, deployment bindings, and publication are managed by proxy administrators."
      />
    );
  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Layers />
            Resource pools
          </PageHeaderTitle>
          <PageHeaderDescription>
            Organize resource sources, upstream deployments, access, and negotiated model pricing.
          </PageHeaderDescription>
        </PageHeader>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => setShowGuide(!showGuide)}>
            <BookOpen />
            {showGuide ? "Hide guide" : "Getting started"}
          </Button>
          <Button onClick={() => setEditing({})}>
            <Plus />
            New resource pool
          </Button>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {[
          {
            label: "Resource pools",
            value: String(items.length),
            description: "Explicitly configured source groups",
            icon: Layers,
          },
          {
            label: "Published",
            value: String(published.length),
            description: `${items.length - published.length} drafts kept unavailable`,
            icon: CheckCircle2,
          },
          {
            label: "Unassigned models",
            value: settings.data ? String(settings.data.unassigned_models.length) : "...",
            description: "Review model coverage before activation",
            icon: CircleAlert,
          },
          {
            label: "Site-wide pool routing",
            value: settings.data?.routing_enabled ? "Enabled" : "Not enabled",
            description: settings.data?.routing_enabled
              ? "Pool routing also applies to legacy keys"
              : "Legacy keys keep their existing routing",
            icon: Route,
          },
        ].map((item) => (
          <Card key={item.label} className="shadow-none" size="sm">
            <CardHeader className="flex flex-row items-center justify-between gap-3">
              <CardDescription>{item.label}</CardDescription>
              <item.icon className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-semibold tracking-tight">{item.value}</p>
              <p className="mt-2 text-xs text-muted-foreground">{item.description}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border bg-muted/20 p-5">
        <div className="flex max-w-4xl items-start gap-3">
          <ShieldCheck className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
          <div>
            <h2 className="text-sm font-medium">
              {settings.data?.routing_enabled
                ? "Site-wide pool routing is active"
                : "Publish pools first, review legacy-key activation separately"}
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {settings.data?.routing_enabled
                ? "Pool routing applies to legacy keys as well as explicitly configured resource-routing keys. Account grants and key policies constrain eligible pairs; retries remain inside the selected pool."
                : "Drafts stay unavailable. Published pools can serve explicitly configured resource-routing keys while site-wide routing is off. Legacy keys keep their existing routing until activation. Review access, coverage, and prices before migrating them."}
            </p>
          </div>
        </div>
        <Button variant="outline" onClick={openActivation} disabled={!settings.data}>
          Review routing settings
          <ArrowRight />
        </Button>
      </div>
      {(showGuide || items.length === 0) && !pools.isPending && (
        <PoolGuide
          progress={{
            pools: items.length,
            boundPools: items.filter((pool) => pool.bindings.length > 0).length,
            publishedPools: published.length,
            routingEnabled: settings.data?.routing_enabled ?? false,
          }}
          onCreate={() => setEditing({})}
          onConfigure={() => {
            const target = items.find((pool) => !pool.enabled) ?? items[0];
            setEditing(target ? { pool: target } : {});
          }}
          onActivate={openActivation}
        />
      )}
      <section className="overflow-hidden rounded-xl border">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b p-4">
          <div className="relative min-w-56 max-w-sm flex-1">
            <Search className="absolute top-2.5 left-3 size-4 text-muted-foreground" />
            <Input
              aria-label="Search resource pools"
              className="pl-9"
              placeholder="Search pool name, ID, or source..."
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <Button
            variant="outline"
            size="sm"
            disabled={pools.isFetching}
            onClick={() => {
              void pools.refetch();
              void settings.refetch();
            }}
          >
            <RefreshCw className={pools.isFetching ? "animate-spin" : ""} />
            Refresh
          </Button>
        </div>
        {pools.isPending ? (
          <LoadingState
            title="Loading resource pools"
            description="Retrieving source groups and deployment bindings."
          />
        ) : pools.error ? (
          <StateMessage
            role="alert"
            tone="destructive"
            icon={<CircleAlert />}
            title="Unable to load resource pools"
            description={pools.error.message}
          />
        ) : filtered.length === 0 ? (
          <StateMessage
            role="status"
            icon={<Layers />}
            title={items.length ? "No matching pools" : "Create your first resource pool"}
            description={
              items.length
                ? "Try another pool name, source, or ID."
                : "Start with a blank pool or choose a template below. No existing deployment is assigned automatically."
            }
          />
        ) : (
          <Table>
            <TableHeader className="bg-muted/40">
              <TableRow>
                <TableHead>Resource pool</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Access</TableHead>
                <TableHead>Models / Deployments</TableHead>
                <TableHead>Default price</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((pool) => (
                <TableRow key={pool.id}>
                  <TableCell>
                    <p className="font-medium">{pool.name}</p>
                    <p className="mt-1 max-w-56 break-all whitespace-normal font-mono text-[10px] text-muted-foreground">
                      {pool.id}
                    </p>
                  </TableCell>
                  <TableCell className="text-xs">{sourceKindLabel(pool.source_kind)}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{pool.visibility === "public" ? "Public" : "Restricted"}</Badge>
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">
                    {pool.models.length} models · {pool.bindings.length} deployments
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">
                    {discountToPercent(pool.default_discount)}% of reference
                  </TableCell>
                  <TableCell>
                    <Badge variant={pool.enabled ? "secondary" : "outline"}>
                      {pool.enabled ? "Published" : "Draft"}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="xs" onClick={() => setPreview(pool)} disabled={!pool.enabled}>
                        Preview route
                      </Button>
                      <Button variant="outline" size="xs" onClick={() => setEditing({ pool })}>
                        Edit
                      </Button>
                      {pool.enabled && (
                        <Button
                          variant="ghost"
                          size="xs"
                          onClick={() => {
                            setDisabling(pool);
                            disable.reset();
                          }}
                        >
                          Unpublish
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>
      {settings.error && (
        <Alert variant="error">
          <AlertTitle>Unable to load routing settings</AlertTitle>
          <AlertDescription>{settings.error.message}</AlertDescription>
        </Alert>
      )}
      <section className="rounded-xl border p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-medium">Start from a source template</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Templates only prefill the setup wizard. You choose bindings, pricing, access, and whether to publish.
            </p>
          </div>
          <Badge variant="outline">No automatic changes</Badge>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {poolTemplates.map((template) => (
            <button
              key={template.name}
              type="button"
              onClick={() => setEditing({ seed: template })}
              className="flex items-center justify-between gap-3 rounded-lg border p-4 text-left text-sm font-medium transition-colors hover:bg-muted/30"
            >
              <span>{template.name}</span>
              <Plus className="size-4 text-muted-foreground" />
            </button>
          ))}
        </div>
      </section>
      <p className="text-xs text-muted-foreground">
        Customer access and pool-specific discounts are managed in{" "}
        <Link className="underline underline-offset-4" href={uiHref("wallet-admin")}>
          Customer Wallets
        </Link>
        . API keys can further restrict permitted pool-model pairs.
      </p>
      {editing && (
        <PoolWizard
          key={editing.pool?.id ?? editing.seed?.source_kind ?? "new"}
          accessToken={accessToken}
          pool={editing.pool}
          seed={editing.seed}
          onClose={() => setEditing(null)}
        />
      )}
      {preview && <PoolPreview accessToken={accessToken} pool={preview} onClose={() => setPreview(null)} />}
      <Dialog
        open={activationOpen}
        onOpenChange={(open) => {
          if (!activation.isPending) setActivationOpen(open);
        }}
      >
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Review resource pool routing</DialogTitle>
            <DialogDescription>
              {settings.data?.routing_enabled
                ? "Disabling site-wide routing restores native routing for legacy keys. Explicitly configured resource-routing keys continue to use published pools."
                : "Site-wide activation also applies pool routing to legacy keys. Explicitly configured resource-routing keys can already use published pools while this setting is off."}
            </DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-3 rounded-lg border p-4 text-sm">
            <div>
              <p className="text-xs text-muted-foreground">Published resource pools</p>
              <p className="mt-1 font-medium">{published.length}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">Models without a published pool</p>
              <p className="mt-1 font-medium">{settings.data?.unassigned_models.length ?? 0}</p>
            </div>
          </div>
          {Boolean(settings.data?.unassigned_models.length) && (
            <div className="rounded-lg border p-4">
              <p className="text-sm font-medium">Unassigned models</p>
              <p className="mt-1 text-xs text-muted-foreground">
                These models have no published pool route. They will not gain access through legacy routing when pool
                routing is enabled.
              </p>
              <div className="mt-3 flex max-h-32 flex-wrap gap-2 overflow-y-auto">
                {settings.data?.unassigned_models.map((model) => (
                  <Badge key={model} variant="outline">
                    {model}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          <p className="text-sm leading-relaxed text-muted-foreground">
            Legacy model-only discount rules are kept in the Legacy / unpooled view. Published pools use their own
            customer-model, plan-model, customer-pool, plan-pool, and pool default pricing rules. Review these rules
            before activation.
          </p>
          <label className="flex items-start gap-3 rounded-lg border p-4 text-sm">
            <Checkbox
              aria-label="I reviewed pool access, model coverage, and pricing"
              checked={activationReviewed}
              onCheckedChange={(checked) => setActivationReviewed(checked === true)}
            />
            <span>I reviewed pool access, model coverage, and pricing.</span>
          </label>
          {activation.error && (
            <Alert variant="error">
              <AlertTitle>{activation.error.message}</AlertTitle>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={activation.isPending} onClick={() => setActivationOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={
                !activationReviewed ||
                activation.isPending ||
                (!settings.data?.routing_enabled && published.length === 0)
              }
              onClick={() => activation.mutate(!settings.data?.routing_enabled)}
            >
              {activation.isPending
                ? "Saving..."
                : settings.data?.routing_enabled
                  ? "Disable pool routing"
                  : "Enable pool routing"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={disabling !== null}
        onOpenChange={(open) => {
          if (!open && !disable.isPending) setDisabling(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Unpublish {disabling?.name}?</DialogTitle>
            <DialogDescription>
              This pool becomes unavailable for new customer requests. Keys fixed to this pool will no longer have an
              eligible route here.
            </DialogDescription>
          </DialogHeader>
          {disable.error && (
            <Alert variant="error">
              <AlertTitle>{disable.error.message}</AlertTitle>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDisabling(null)} disabled={disable.isPending}>
              Cancel
            </Button>
            <Button
              disabled={disable.isPending}
              onClick={() => {
                if (disabling) disable.mutate(disabling);
              }}
            >
              Unpublish pool
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
