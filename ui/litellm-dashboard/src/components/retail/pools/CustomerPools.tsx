"use client";

import { useQuery } from "@tanstack/react-query";
import { Layers } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { uiHref } from "@/utils/uiHref";
import { retailRequest } from "../api";
import { PoolCatalog, sourceKindLabel } from "./types";

export default function CustomerPools({
  accessToken,
  userId,
  enabled,
}: {
  accessToken: string | null;
  userId: string | null;
  enabled: boolean;
}) {
  const pools = useQuery({
    queryKey: ["retail", "pools", "self", userId],
    queryFn: () => retailRequest<PoolCatalog>("/pools", accessToken),
    enabled: enabled && Boolean(accessToken) && Boolean(userId),
  });
  return (
    <section id="resource-pool-guide" className="scroll-mt-6 rounded-xl border p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-medium">
            <Layers className="size-4" />
            Your resource pools
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Your account determines the allowed pool-model pairs. API keys can narrow this access.
          </p>
        </div>
        <Badge variant="outline">
          {pools.data?.routing_enabled ? "Site-wide routing active" : "Explicit key policies supported"}
        </Badge>
      </div>
      {pools.isPending ? (
        <p className="mt-4 text-sm text-muted-foreground" role="status">
          Loading available resource pools...
        </p>
      ) : pools.error ? (
        <p className="mt-4 text-sm text-destructive" role="alert">
          {pools.error.message}
        </p>
      ) : pools.data?.items.length ? (
        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {pools.data.items.map((pool) => (
            <div className="rounded-lg border bg-muted/10 p-4" key={pool.id}>
              <p className="text-sm font-medium">{pool.name}</p>
              <p className="mt-1 text-xs text-muted-foreground">{sourceKindLabel(pool.source_kind)}</p>
              <div className="mt-3 flex flex-wrap gap-1">
                {(pool.allowed_models ?? pool.models).map((model) => (
                  <Badge className="max-w-full break-all whitespace-normal" key={model} variant="outline">
                    {model}
                  </Badge>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-4 text-sm text-muted-foreground">
          No published resource pools are available to this account. Contact your administrator for access.
        </p>
      )}
      <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
        Use Automatic to follow account grants, Fixed to use one permitted pool, or Custom to select exact pairs.{" "}
        <Link className="underline underline-offset-4" href={uiHref("api-keys")}>
          Manage virtual keys
        </Link>
      </p>
    </section>
  );
}
