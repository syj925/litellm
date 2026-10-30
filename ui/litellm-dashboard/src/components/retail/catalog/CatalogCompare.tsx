"use client";

import type { ReactNode } from "react";
import { Logo } from "@/components/molecules/logo/Logo";
import CopyButton from "@/components/shared/CopyButton";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatContext, getModelIdentity } from "./modelIdentity";
import {
  formatCatalogPrice,
  modelPrice,
  type CatalogModel,
  type CatalogPriceBasis,
  type CatalogTokenPrice,
} from "./types";
import SpecificationCharts from "./SpecificationCharts";
import ModelBenchmarks from "./ModelBenchmarks";
import { cacheDurationLabel, cacheTierEntries } from "../cachePricing";

interface CatalogCompareProps {
  models: CatalogModel[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  basis: CatalogPriceBasis;
  publicView?: boolean;
}

const capabilityLabel = (value: boolean | null): string => {
  if (value == null) return "Not specified";
  return value ? "Supported" : "Not supported";
};

const modalityLabel = (values: string[]): string =>
  values.length ? values.map((value) => value.charAt(0).toUpperCase() + value.slice(1)).join(", ") : "Not specified";

function priceLabel(model: CatalogModel, basis: CatalogPriceBasis, field: "input" | "output"): string {
  const price = modelPrice(model, basis, field);
  return price == null ? "Not listed" : `From ${formatCatalogPrice(String(price))}`;
}

function cachePriceCards(model: CatalogModel, basis: CatalogPriceBasis): CatalogTokenPrice[] {
  const prices = model.offers.flatMap((offer) => {
    const price = offer.pricing[basis];
    return price && (basis === "reference" || offer.available_to_me) ? [price] : [];
  });
  return prices.length || basis === "customer" || !model.reference_pricing ? prices : [model.reference_pricing];
}

function defaultCachePrice(model: CatalogModel, basis: CatalogPriceBasis): string | null {
  const rates = cachePriceCards(model, basis).flatMap((price) =>
    cacheTierEntries(price.cache_write_tiers).length || price.cache_write == null || price.cache_write.trim() === ""
      ? []
      : [Number(price.cache_write)],
  ).filter((price) => Number.isFinite(price) && price >= 0);
  return rates.length ? `From ${formatCatalogPrice(String(Math.min(...rates)))}` : null;
}

interface CompareRow {
  label: string;
  value: (model: CatalogModel) => ReactNode;
  basis?: CatalogPriceBasis;
  personal?: boolean;
}

const rows: CompareRow[] = [
  { label: "Author", value: (model) => getModelIdentity(model.id, model.provider).author },
  {
    label: "Your access",
    personal: true,
    value: (model) => <Badge variant="outline">{model.available_to_me ? "Available" : "Access required"}</Badge>,
  },
  { label: "Input context window", value: (model) => formatContext(model.context_length) },
  { label: "Total context window", value: (model) => formatContext(model.context_window) },
  { label: "Maximum output", value: (model) => formatContext(model.max_output_tokens) },
  { label: "Input modalities", value: (model) => modalityLabel(model.input_modalities) },
  { label: "Output modalities", value: (model) => modalityLabel(model.output_modalities) },
  { label: "Tool calling", value: (model) => capabilityLabel(model.capabilities.tools) },
  { label: "Vision", value: (model) => capabilityLabel(model.capabilities.vision) },
  { label: "Reasoning", value: (model) => capabilityLabel(model.capabilities.reasoning) },
  { label: "Prompt caching", value: (model) => capabilityLabel(model.capabilities.prompt_caching) },
  { label: "Structured output", value: (model) => capabilityLabel(model.capabilities.structured_output) },
  {
    label: "Reference input / 1M tokens",
    value: (model) => priceLabel(model, "reference", "input"),
    basis: "reference",
  },
  {
    label: "Reference output / 1M tokens",
    value: (model) => priceLabel(model, "reference", "output"),
    basis: "reference",
  },
  { label: "Your input / 1M tokens", value: (model) => priceLabel(model, "customer", "input"), basis: "customer" },
  { label: "Your output / 1M tokens", value: (model) => priceLabel(model, "customer", "output"), basis: "customer" },
  { label: "Resource pools", value: (model) => model.offers.length },
];

export default function CatalogCompare({ models, open, onOpenChange, basis, publicView = false }: CatalogCompareProps) {
  const defaultCacheRows: CompareRow[] = (["reference", "customer"] as const)
    .filter((priceBasis) => models.some((model) => defaultCachePrice(model, priceBasis) != null))
    .map((priceBasis) => ({
      label: `${priceBasis === "reference" ? "Reference" : "Your"} cache write / 1M tokens`,
      basis: priceBasis,
      value: (model) => defaultCachePrice(model, priceBasis) ?? (
        cachePriceCards(model, priceBasis).some((price) => cacheTierEntries(price.cache_write_tiers).length)
          ? "See duration rates"
          : "Not listed"
      ),
    }));
  const durations = [
    ...new Set(
      models.flatMap((model) => [
        ...cacheTierEntries(model.reference_pricing?.cache_write_tiers).map(([ttl]) => ttl),
        ...model.offers.flatMap((offer) =>
          cacheTierEntries(offer.pricing.reference?.cache_write_tiers).map(([ttl]) => ttl),
        ),
      ]),
    ),
  ].sort((left, right) => Number(left) - Number(right));
  const cacheRows: CompareRow[] = durations.flatMap((ttl) =>
    (["reference", "customer"] as const).map((priceBasis) => ({
      label: `${priceBasis === "reference" ? "Reference" : "Your"} cache write · ${cacheDurationLabel(ttl)} / 1M tokens`,
      basis: priceBasis,
      value: (model: CatalogModel) => {
        const rates: (string | undefined)[] = model.offers.flatMap((offer) =>
          priceBasis === "customer" && !offer.available_to_me
            ? []
            : [offer.pricing[priceBasis]?.cache_write_tiers?.[ttl]],
        );
        const candidates =
          priceBasis === "reference" && !rates.some((value) => value != null)
            ? [...rates, model.reference_pricing?.cache_write_tiers?.[ttl]]
            : rates;
        const listed = candidates.flatMap((value) =>
          value != null && value.trim() !== "" && Number.isFinite(Number(value)) && Number(value) >= 0
            ? [Number(value)]
            : [],
        );
        return listed.length ? `From ${formatCatalogPrice(String(Math.min(...listed)))}` : "Not listed";
      },
    })),
  );
  const visibleRows = [...rows, ...defaultCacheRows, ...cacheRows].filter(
    (row) => !publicView || (!row.personal && row.basis !== "customer"),
  );
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] min-w-0 grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden sm:w-[calc(100vw-3rem)] sm:max-w-6xl">
        <DialogHeader className="pr-8">
          <DialogTitle>Compare models</DialogTitle>
          <DialogDescription>
            {publicView
              ? "Compare publicly listed model specifications and reference rates."
              : "Reference and customer prices are shown separately. Highlighted rates match your selected price view."}
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 min-w-0 max-h-[65dvh] overflow-auto rounded-md border">
          <SpecificationCharts models={models} />
          {models.some((model) => model.benchmarks?.length) && (
            <div className="border-b px-4 pb-4">
              <ModelBenchmarks models={models} />
            </div>
          )}
          <Table className="min-w-[44rem] table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="sticky left-0 z-sticky-pinned w-44 bg-popover align-top">Specification</TableHead>
                {models.map((model) => {
                  const identity = getModelIdentity(model.id, model.provider);
                  const displayName = model.name && model.name !== model.id ? model.name : identity.displayName;
                  return (
                    <TableHead key={model.id} className="px-4 py-4 align-top whitespace-normal">
                      <div className="flex items-center gap-2 text-base font-medium text-foreground">
                        <Logo provider={identity.providerSlug} label={identity.author} className="size-6 shrink-0" />
                        <span>{displayName}</span>
                      </div>
                      <div className="mt-2 flex items-start gap-1 font-mono text-xs font-normal text-muted-foreground">
                        <span className="min-w-0 break-all">{model.id}</span>
                        <CopyButton value={model.id} label={`Copy model ID ${model.id}`} className="shrink-0" />
                      </div>
                    </TableHead>
                  );
                })}
              </TableRow>
            </TableHeader>
            <TableBody>
              {visibleRows.map((row) => (
                <TableRow
                  key={row.label}
                  className={row.basis === (publicView ? "reference" : basis) ? "bg-muted/40" : undefined}
                >
                  <TableHead
                    scope="row"
                    className="sticky left-0 z-sticky-pinned bg-popover text-xs font-medium whitespace-normal"
                  >
                    {row.label}
                  </TableHead>
                  {models.map((model) => (
                    <TableCell key={model.id} className="px-4 py-3 whitespace-normal">
                      {row.value(model)}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          {publicView
            ? "Reference rates are base prices from the published model catalog. See model details for resource options and pricing tiers."
            : "Prices are the lowest listed rates across each model's resource pools. See model details for each pool's pricing and access."}{" "}
          Unlisted specifications and prices are not inferred.
        </p>
      </DialogContent>
    </Dialog>
  );
}
