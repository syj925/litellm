import { ArrowUpRight, Box, CheckCircle2 } from "lucide-react";
import { Logo } from "@/components/molecules/logo/Logo";
import CopyButton from "@/components/shared/CopyButton";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { catalogDescription, capabilityOptions } from "./catalogState";
import { getModelIdentity, formatContext, catalogModelTitle } from "./modelIdentity";
import { formatCatalogPrice, modelPrice, type CatalogModel, type CatalogPriceBasis } from "./types";
import { MetadataSourceSummary } from "./MetadataSources";

interface Props {
  models: CatalogModel[];
  basis: CatalogPriceBasis;
  view: "list" | "table";
  publicView?: boolean;
  selectedIds: string[];
  onToggle: (id: string) => void;
  onOpen: (id: string) => void;
}

function ModelLogo({ model }: { model: CatalogModel }) {
  const identity = getModelIdentity(model.id, model.provider);
  return identity.providerSlug ? (
    <Logo provider={identity.providerSlug} className="size-6 shrink-0 object-contain" />
  ) : (
    <Box className="size-6 shrink-0 text-muted-foreground" />
  );
}

function Availability({ model }: { model: CatalogModel }) {
  return model.available_to_me ? (
    <span className="inline-flex items-center gap-1 text-xs text-emerald-700 dark:text-emerald-400">
      <CheckCircle2 className="size-3.5" />
      Available to you
    </span>
  ) : (
    <span className="text-xs text-muted-foreground">Access required</span>
  );
}

function priceLabel(model: CatalogModel, basis: CatalogPriceBasis, field: "input" | "output"): string {
  const value = modelPrice(model, basis, field);
  return value == null ? "Not listed" : `From ${formatCatalogPrice(String(value))}`;
}

export default function CatalogModelList({
  models,
  basis,
  view,
  publicView = false,
  selectedIds,
  onToggle,
  onOpen,
}: Props) {
  const compareCheck = (model: CatalogModel) => (
    <Checkbox
      checked={selectedIds.includes(model.id)}
      disabled={selectedIds.length >= 3 && !selectedIds.includes(model.id)}
      aria-label={`Compare ${model.id}`}
      onCheckedChange={() => onToggle(model.id)}
    />
  );
  if (view === "table")
    return (
      <div className="overflow-x-auto rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-9">Compare</TableHead>
              <TableHead>Model</TableHead>
              <TableHead>Input context</TableHead>
              <TableHead>Input / M</TableHead>
              <TableHead>Output / M</TableHead>
              <TableHead>{publicView ? "Resources" : "Availability"}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {models.map((model) => (
              <TableRow key={model.id}>
                <TableCell>{compareCheck(model)}</TableCell>
                <TableCell>
                  <button
                    onClick={() => onOpen(model.id)}
                    className="flex items-center gap-3 text-left hover:underline"
                  >
                    <ModelLogo model={model} />
                    <span>
                      <span className="block font-medium">{catalogModelTitle(model)}</span>
                      <span className="block font-mono text-xs text-muted-foreground">{model.id}</span>
                    </span>
                  </button>
                  <MetadataSourceSummary model={model} />
                </TableCell>
                <TableCell>{formatContext(model.context_length)}</TableCell>
                <TableCell>{priceLabel(model, basis, "input")}</TableCell>
                <TableCell>{priceLabel(model, basis, "output")}</TableCell>
                <TableCell>{publicView ? `${model.offers.length} pools` : <Availability model={model} />}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    );
  return (
    <div className="space-y-3">
      {models.map((model) => {
        const identity = getModelIdentity(model.id, model.provider);
        return (
          <article
            key={model.id}
            className="rounded-lg border bg-card p-5 transition-colors hover:border-foreground/25"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3">
                <ModelLogo model={model} />
                <div className="min-w-0">
                  <button
                    onClick={() => onOpen(model.id)}
                    className="group flex items-center gap-1.5 text-left text-base font-semibold hover:underline"
                  >
                    {catalogModelTitle(model)}
                    <ArrowUpRight className="size-3.5 shrink-0 text-muted-foreground opacity-0 group-hover:opacity-100" />
                  </button>
                  <div className="mt-0.5 flex items-center gap-1 text-xs text-muted-foreground">
                    <span className="truncate font-mono" title={model.id}>
                      {model.id}
                    </span>
                    <CopyButton value={model.id} label={`Copy model ID ${model.id}`} />
                  </div>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                {!publicView && (
                  <span className="hidden sm:inline">
                    <Availability model={model} />
                  </span>
                )}
                {compareCheck(model)}
              </div>
            </div>
            <p className="my-3 line-clamp-2 text-sm leading-6 text-muted-foreground">{catalogDescription(model)}</p>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
              <span>
                by <span className="font-medium text-foreground">{identity.author}</span>
              </span>
              <span>{formatContext(model.context_length)} input context</span>
              {model.context_window != null && <span>{formatContext(model.context_window)} total context</span>}
              <span>
                <span className="font-medium text-foreground">{priceLabel(model, basis, "input")}</span> / M input
              </span>
              <span>
                <span className="font-medium text-foreground">{priceLabel(model, basis, "output")}</span> / M output
              </span>
              <button className="hover:text-foreground hover:underline" onClick={() => onOpen(model.id)}>
                {publicView && !model.offers.length
                  ? "View model"
                  : `${model.offers.length} resource ${model.offers.length === 1 ? "pool" : "pools"}`}
              </button>
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {capabilityOptions
                .filter(({ id }) => model.capabilities[id] === true)
                .slice(0, 5)
                .map(({ id, label }) => (
                  <Badge key={id} variant="secondary" className="text-[10px] font-normal">
                    {label}
                  </Badge>
                ))}
              {model.offers.slice(0, 3).map((offer) => (
                <Badge key={offer.pool_id} variant="outline" className="text-[10px] font-normal text-muted-foreground">
                  {offer.pool_name}
                </Badge>
              ))}
            </div>
            <MetadataSourceSummary model={model} />
          </article>
        );
      })}
    </div>
  );
}
