"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe2, LockKeyhole, ShieldCheck } from "lucide-react";
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
import { Input } from "@/components/ui/input";
import { toast } from "@/lib/toast";
import { retailRequest } from "../api";
import { PoolAccess, ResourcePool, sourceKindLabel } from "./types";
import { updatePoolPair } from "./policy";

export default function PoolAccessEditor({
  accessToken,
  userId,
  onClose,
}: {
  accessToken: string | null;
  userId: string;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Pick<PoolAccess, "mode" | "grants"> | null>(null);
  const [search, setSearch] = useState("");
  const policy = useQuery({
    queryKey: ["retail", "pool-access", userId],
    queryFn: () => retailRequest<PoolAccess>(`/admin/wallets/${encodeURIComponent(userId)}/pool-access`, accessToken),
    enabled: Boolean(accessToken),
  });
  const pools = useQuery({
    queryKey: ["retail", "pools", "admin"],
    queryFn: () => retailRequest<{ items: ResourcePool[] }>("/admin/pools", accessToken),
    enabled: Boolean(accessToken),
  });
  const current = draft ?? policy.data ?? { mode: "inherit" as const, grants: [] };
  const save = useMutation({
    mutationFn: () =>
      retailRequest<PoolAccess>(`/admin/wallets/${encodeURIComponent(userId)}/pool-access`, accessToken, {
        method: "PUT",
        body: JSON.stringify({ mode: current.mode, grants: current.mode === "inherit" ? [] : current.grants }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail"] });
      toast.success("Account resource pool access saved");
      onClose();
    },
  });
  const filtered =
    pools.data?.items.filter((pool) =>
      `${pool.name} ${pool.id} ${pool.models.join(" ")}`.toLowerCase().includes(search.toLowerCase()),
    ) ?? [];
  const pairCount = current.grants.reduce(
    (total, grant) =>
      total +
      (grant.models === "*"
        ? pools.data?.items.find((pool) => pool.id === grant.pool_id)?.models.length ?? 0
        : grant.models.length),
    0,
  );
  const error = policy.error ?? pools.error;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) onClose();
      }}
    >
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>Account resource pool access</DialogTitle>
          <DialogDescription className="break-all">{userId}</DialogDescription>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          Set which pool-model pairs this account may use. API keys can narrow this access but cannot expand it.
          Discount rules do not grant access.
        </p>
        {policy.isPending || pools.isPending ? (
          <LoadingState
            title="Loading pool access"
            description="Retrieving account grants and available source pools."
          />
        ) : error ? (
          <Alert variant="error">
            <AlertTitle>{error.message}</AlertTitle>
          </Alert>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                {
                  value: "inherit" as const,
                  title: "Inherit published public pools",
                  text: "Includes models from currently published public pools and future public pool updates.",
                  icon: Globe2,
                },
                {
                  value: "custom" as const,
                  title: "Custom pool-model pairs",
                  text: "Only the explicitly selected pairs below are granted, including restricted pools.",
                  icon: LockKeyhole,
                },
              ].map((item) => (
                <button
                  type="button"
                  key={item.value}
                  aria-label={item.title}
                  disabled={save.isPending}
                  onClick={() => setDraft({ ...current, mode: item.value })}
                  aria-pressed={current.mode === item.value}
                  className={`rounded-xl border p-4 text-left ${current.mode === item.value ? "border-foreground/30 bg-muted/35" : "hover:bg-muted/15"}`}
                >
                  <item.icon className="mb-3 size-5 text-muted-foreground" />
                  <p className="text-sm font-medium">{item.title}</p>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{item.text}</p>
                </button>
              ))}
            </div>
            {current.mode === "inherit" ? (
              <div className="rounded-lg border bg-muted/20 p-4">
                <p className="text-sm font-medium">Published public pools</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {pools.data?.items
                    .filter((pool) => pool.enabled && pool.visibility === "public")
                    .map((pool) => (
                      <Badge variant="outline" key={pool.id}>
                        {pool.name} · {pool.models.length} models
                      </Badge>
                    ))}
                  {!pools.data?.items.some((pool) => pool.enabled && pool.visibility === "public") && (
                    <p className="text-xs text-muted-foreground">No public pools are published yet.</p>
                  )}
                </div>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <Input
                    aria-label="Search account pool access"
                    disabled={save.isPending}
                    className="max-w-sm"
                    placeholder="Search pool or public model..."
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                  <span className="text-xs text-muted-foreground">{pairCount} pool-model pairs selected</span>
                </div>
                <div className="max-h-96 space-y-3 overflow-y-auto">
                  {filtered.map((pool) => {
                    const grant = current.grants.find((item) => item.pool_id === pool.id);
                    const query = search.toLowerCase();
                    const models = `${pool.name} ${pool.id}`.toLowerCase().includes(query)
                      ? pool.models
                      : pool.models.filter((model) => model.toLowerCase().includes(query));
                    return (
                      <div className="rounded-xl border p-4" key={pool.id}>
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <h3 className="text-sm font-medium">{pool.name}</h3>
                            <p className="mt-1 text-xs text-muted-foreground">
                              {sourceKindLabel(pool.source_kind)} · {pool.enabled ? "Published" : "Draft, unavailable"}{" "}
                              · {pool.visibility}
                            </p>
                          </div>
                          <label className="flex items-center gap-2 text-xs">
                            <Checkbox
                              aria-label={`Allow all current and future models in ${pool.name}`}
                              disabled={save.isPending}
                              checked={grant?.models === "*"}
                              onCheckedChange={(checked) =>
                                setDraft({
                                  ...current,
                                  grants: checked
                                    ? [
                                        ...current.grants.filter((item) => item.pool_id !== pool.id),
                                        { pool_id: pool.id, models: "*" },
                                      ]
                                    : current.grants.filter((item) => item.pool_id !== pool.id),
                                })
                              }
                            />
                            All current and future models
                          </label>
                        </div>
                        <div className="mt-4 grid gap-3 sm:grid-cols-2">
                          {models.map((model) => (
                            <label key={model} className="flex items-start gap-2 text-xs">
                              <Checkbox
                                aria-label={`Allow ${model} in ${pool.name}`}
                                disabled={save.isPending}
                                checked={grant?.models === "*" || Boolean(grant?.models.includes(model))}
                                onCheckedChange={(checked) =>
                                  setDraft({
                                    ...current,
                                    grants: updatePoolPair(current.grants, pool, model, checked === true),
                                  })
                                }
                              />
                              <span className="break-all">{model}</span>
                            </label>
                          ))}
                          {models.length === 0 && (
                            <p className="text-xs text-muted-foreground">No matching models in this pool.</p>
                          )}
                        </div>
                      </div>
                    );
                  })}
                  {filtered.length === 0 && (
                    <p className="rounded-lg border p-6 text-center text-sm text-muted-foreground">
                      No resource pools match this search.
                    </p>
                  )}
                </div>
              </>
            )}
            <p className="rounded-lg border bg-muted/20 p-4 text-xs leading-relaxed text-muted-foreground">
              <ShieldCheck className="mr-1 inline size-3" />
              Selections are stored as paired grants. Choosing Model A in one pool and Model B in another never grants
              the other combinations. Draft pools stay unavailable even when a grant exists.
            </p>
          </>
        )}
        {save.error && (
          <Alert variant="error">
            <AlertTitle>Access was not saved</AlertTitle>
            <AlertDescription>{save.error.message}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={save.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={policy.isPending || pools.isPending || Boolean(error) || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving..." : "Save account pool access"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
