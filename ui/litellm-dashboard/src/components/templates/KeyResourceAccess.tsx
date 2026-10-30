import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import {
  readResourcePolicy,
  resourcePolicyPoolModels,
  type KeyResourcePool,
  type ResourceRoutingPolicy,
} from "../organisms/resourceRoutingPolicy";
import { useKeyResourceCatalog } from "../organisms/useKeyResourceCatalog";
import { sourceKindLabel } from "../retail/pools/types";
import { cn } from "@/lib/cva.config";

interface Props {
  accessToken: string | null;
  userId: string | null;
  admin: boolean;
  metadata: unknown;
  models: readonly string[] | null | undefined;
  className?: string;
}

export default function KeyResourceAccess({ accessToken, userId, admin, metadata, models, className }: Props) {
  const savedPolicy = readResourcePolicy(metadata);
  const catalog = useKeyResourceCatalog({ accessToken, targetUserId: userId, admin });
  const policy = savedPolicy ?? (catalog.data?.routing_enabled ? { mode: "auto" as const } : null);
  if (!policy) return null;
  const modeLabel = { fixed: "Fixed pool", custom: "Custom pools", auto: "Automatic" }[policy.mode];

  return (
    <section aria-label="Key resource access" className={cn("space-y-4", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-medium">Resource access</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            {policy.mode === "fixed"
              ? "Requests stay in the fixed pool, with no fallback to other pools."
              : "Requests use the pool and model permissions granted to this account and key."}
          </p>
        </div>
        <Badge variant="secondary">{modeLabel}</Badge>
      </div>
      {catalog.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          Verifying resource access...
        </p>
      )}
      {!catalog.isPending && catalog.error && (
        <Alert variant="error">
          <AlertTitle>Resource access could not be verified</AlertTitle>
          <AlertDescription>
            The saved routing policy is retained. Refresh to check its current pools and models.
          </AlertDescription>
        </Alert>
      )}
      {!catalog.isPending && !catalog.error && (
        <ResourceAccessModels policy={policy} catalogPools={catalog.data?.items ?? []} models={models ?? []} />
      )}
      <p className="text-xs text-muted-foreground">
        Resource source and API integration are separate. A pool with AWS Bedrock resources can use an
        Anthropic-compatible API.
      </p>
    </section>
  );
}

function ResourceAccessModels({
  policy,
  catalogPools,
  models,
}: {
  policy: ResourceRoutingPolicy;
  catalogPools: KeyResourcePool[];
  models: readonly string[];
}) {
  const unrestricted = !models.length || models.includes("all-proxy-models") || models.includes("*");
  const pools = resourcePolicyPoolModels(policy, catalogPools).map(({ pool, models: poolModels }) => ({
    pool,
    models: unrestricted ? poolModels : poolModels.filter((model) => models.includes(model)),
  }));
  const modelCount = new Set(pools.flatMap((item) => item.models)).size;
  const fixedPool = policy.mode === "fixed" ? catalogPools.find((pool) => pool.id === policy.pool_id) : null;

  return (
    <>
      {policy.mode === "fixed" && !pools.length && (
        <p className="text-sm font-medium">{fixedPool?.name ?? policy.pool_id}</p>
      )}
      <p className="text-sm text-muted-foreground">
        {modelCount} currently available models across {pools.length} authorized pools
        {unrestricted
          ? ". No additional model restriction is set on this key."
          : ". Key model restrictions also apply."}
      </p>
      {pools.map(({ pool, models: poolModels }) => (
        <div key={pool.id} className="rounded-md border p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{pool.name}</span>
            {pool.source_kind && <Badge variant="outline">{sourceKindLabel(pool.source_kind)}</Badge>}
          </div>
          <div className="flex flex-wrap gap-2">
            {poolModels.map((model) => (
              <Badge key={model} variant="secondary">
                {model}
              </Badge>
            ))}
            {!poolModels.length && (
              <span className="text-sm text-muted-foreground">No models match this key&apos;s restrictions.</span>
            )}
          </div>
        </div>
      ))}
      {!pools.length && (
        <p role="status" className="text-sm text-destructive">
          No authorized live models are available for this routing policy.
        </p>
      )}
    </>
  );
}
