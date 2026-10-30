"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, RefreshCw } from "lucide-react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { isProxyAdminRole } from "@/utils/roles";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { retailRequest, RetailRequestError } from "../api";

export interface CatalogSourceStatus {
  source: "models_dev" | "openrouter";
  label: string;
  url: string;
  last_attempt_at: string | null;
  last_success_at: string | null;
  record_count: number;
  last_error: string | null;
  stale: boolean;
}

interface CatalogSourcesStatus {
  sources: CatalogSourceStatus[];
  syncing: boolean;
  refresh_interval_seconds: number;
}

function statusDate(value: string | null): string {
  if (!value) return "Not synced yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleString(undefined, { timeZoneName: "short" });
}

function sourceLabel(source: CatalogSourceStatus["source"]): string {
  return source === "models_dev" ? "models.dev" : "OpenRouter";
}

function statusLabel(source: CatalogSourceStatus): string {
  if (source.last_error) return "Sync issue";
  if (!source.last_success_at) return "Waiting for first sync";
  return source.stale ? "Refresh due" : "Up to date";
}

function SourceCard({ source }: { source: CatalogSourceStatus }) {
  const state = statusLabel(source);
  return (
    <section className="rounded-lg border bg-card p-5" aria-label={`${sourceLabel(source.source)} sync status`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold">{sourceLabel(source.source)}</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            {source.source === "models_dev"
              ? "Model specifications and provider capabilities"
              : "Model descriptions and supported parameters"}
          </p>
        </div>
        <Badge variant="outline">{state}</Badge>
      </div>
      <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-muted-foreground">Saved records</dt>
          <dd className="mt-1 font-medium tabular-nums">{source.record_count.toLocaleString()}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Last successful sync</dt>
          <dd className="mt-1">{statusDate(source.last_success_at)}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-xs text-muted-foreground">Last attempt</dt>
          <dd className="mt-1">{statusDate(source.last_attempt_at)}</dd>
        </div>
      </dl>
      {source.last_error && (
        <p className="mt-4 rounded-md border border-destructive/20 bg-destructive/5 px-3 py-2 text-xs leading-5 text-destructive">
          {source.last_error}{" "}
          {source.last_success_at
            ? "The last successful snapshot remains available."
            : "No saved snapshot is available yet."}
        </p>
      )}
      <a
        href={
          source.source === "models_dev"
            ? "https://models.dev"
            : "https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties"
        }
        target="_blank"
        rel="noopener noreferrer"
        className="mt-4 inline-block text-xs font-medium hover:underline"
      >
        View data source
      </a>
    </section>
  );
}

export default function CatalogSourcesPanel() {
  const { accessToken, userRole, isViewOnly } = useAuthorized();
  const allowed = isProxyAdminRole(userRole) && !isViewOnly;
  const client = useQueryClient();
  const queryKey = ["retail", "catalog-sources", accessToken];
  const statusOptions = {
    queryKey,
    queryFn: () => retailRequest<CatalogSourcesStatus>("/admin/catalog-sources", accessToken),
    enabled: allowed && Boolean(accessToken),
    refetchInterval: 60_000,
  };
  const status = useQuery(statusOptions);
  const sync = useMutation({
    mutationFn: () =>
      retailRequest<CatalogSourcesStatus>("/admin/catalog-sources/sync", accessToken, { method: "POST" }),
    onSuccess: (result) => client.setQueryData(queryKey, result),
    onSettled: () => client.invalidateQueries({ queryKey }),
  });
  if (!allowed) return <p className="text-sm text-muted-foreground">Proxy administrator access is required.</p>;
  return (
    <div className="space-y-5 py-2">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold">
            <Database className="size-5" /> Catalog data
          </h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
            Sync model descriptions, specifications and capability references from models.dev and OpenRouter.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => status.refetch()}
            disabled={status.isFetching || sync.isPending}
          >
            <RefreshCw /> Reload status
          </Button>
          <Button size="sm" onClick={() => sync.mutate()} disabled={sync.isPending || status.data?.syncing}>
            <RefreshCw className={sync.isPending ? "animate-spin" : ""} />
            {sync.isPending || status.data?.syncing ? "Syncing" : "Sync now"}
          </Button>
        </div>
      </div>
      <div className="rounded-lg border bg-muted/30 px-4 py-3 text-sm leading-6 text-muted-foreground">
        Only catalog information is synced. Prices and customer discounts use site configuration. TTFT, success rates
        and other operational metrics use site traffic. A sync does not change channel settings, model access or
        billing.
      </div>
      {status.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading source status...
        </p>
      )}
      {status.isError && (
        <p role="alert" className="text-sm text-destructive">
          Source status could not be loaded. Reload to try again.
        </p>
      )}
      {sync.isError && (
        <p role="alert" className="text-sm text-destructive">
          {sync.error instanceof RetailRequestError && sync.error.status === 409
            ? "A catalog sync is already running. Reload status shortly."
            : "The sync could not be completed. Saved catalog data remains available."}
        </p>
      )}
      {sync.isSuccess && (
        <p role="status" className="text-sm text-muted-foreground">
          {sync.data.syncing
            ? "A catalog sync is already running. Reload status shortly."
            : "Sync completed. Review each source status below."}
        </p>
      )}
      {status.data && (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            {status.data.sources.map((source) => (
              <SourceCard key={source.source} source={source} />
            ))}
          </div>
          <p className="text-xs leading-6 text-muted-foreground">
            Automatic refresh runs every {Math.round(status.data.refresh_interval_seconds / 3600)} hours. A failed
            source keeps its last successful snapshot. Model matching uses exact identities; unavailable fields stay
            unspecified.
          </p>
        </>
      )}
    </div>
  );
}
