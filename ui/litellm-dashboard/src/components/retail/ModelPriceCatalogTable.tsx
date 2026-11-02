"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, CircleAlert, Download, Plus, RefreshCw, Search, Tags } from "lucide-react";
import { useState } from "react";
import { Logo } from "@/components/molecules/logo/Logo";
import { getProviderLogoAndName } from "@/components/provider_info_helpers";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { useUrlTableState } from "@/components/shared/DataTable/useUrlTableState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatMoney, retailRequest } from "./api";
import { cacheWritePriceRows } from "./cachePricing";
import PriceConfigurationReview from "./PriceConfigurationReview";
import ExpressionPricingRule from "./ExpressionPricingRule";
import { baselineTokenPrice, hasAdditionalPriceConditions, priceConditions } from "./priceDisplay";
import {
  displayedModelPrice,
  filterModelPrices,
  pricingPage,
  pricingStatusLabels,
  type ModelPriceCatalogEntry,
  type SalesPrice,
} from "./modelPriceCatalog";

const filterColumns = ["provider", "mode", "status"] as const;
const tableOptions = {
  sortFields: ["model"],
  defaultSort: { id: "model", desc: false },
  defaultPageSize: 25,
  filterColumns,
};
const displayedRateFields = ["input_per_million", "output_per_million", "cache_read_per_million"] as const;
const modeLabels: Readonly<Record<string, string>> = {
  chat: "Chat",
  completion: "Completion",
  embedding: "Embeddings",
  image_generation: "Image generation",
  image_edit: "Image editing",
  audio_transcription: "Transcription",
  audio_speech: "Speech",
  rerank: "Rerank",
};
const modeLabel = (mode: string) => modeLabels[mode] ?? mode.replaceAll("_", " ");
const hasReviewReferences = (entry: ModelPriceCatalogEntry) =>
  entry.status !== "ambiguous" && Boolean(entry.review_references?.length);

function rowActionLabel(entry: ModelPriceCatalogEntry): string {
  if (entry.price?.expression_policy) return "Edit dynamic pricing";
  if (entry.price) return "Edit price";
  if (entry.status === "ambiguous") return "Choose reference";
  if (entry.review_references?.length) return "Review price";
  return "Set price";
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <Select value={value || "all"} onValueChange={(next) => onChange(next === "all" ? "" : next ?? "")}>
      <SelectTrigger aria-label={label} className="min-w-40">
        <SelectValue>
          {value ? options.find((option) => option.value === value)?.label ?? value : `All ${label.toLowerCase()}`}
        </SelectValue>
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="all">All {label.toLowerCase()}</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function PriceRow({
  entry,
  onEdit,
  onChoose,
  onView,
}: {
  entry: ModelPriceCatalogEntry;
  onEdit: (price: SalesPrice) => void;
  onChoose: (entry: ModelPriceCatalogEntry) => void;
  onView: (price: SalesPrice) => void;
}) {
  const price = displayedModelPrice(entry);
  const dynamic = Boolean(price?.expression_policy);
  const baseline = baselineTokenPrice(price?.pricing_display);
  const otherConditions = hasAdditionalPriceConditions(price?.pricing_display);
  return (
    <TableRow>
      <TableCell className="min-w-56 max-w-96 pl-5 whitespace-normal">
        <div className="flex items-center gap-3">
          <Logo provider={entry.providers[0] ?? ""} className="size-7 shrink-0" />
          <div className="min-w-0">
            <p className="break-all font-medium">{entry.model}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {entry.providers.map((provider) => getProviderLogoAndName(provider).displayName).join(", ") ||
                "Provider not specified"}
              {!entry.configured && " · Saved price only"}
            </p>
          </div>
        </div>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {entry.modes.map(modeLabel).join(", ") || "Not specified"}
      </TableCell>
      <TableCell>
        <Badge variant={entry.status === "ambiguous" || entry.status === "unavailable" ? "outline" : "secondary"}>
          {dynamic ? "Dynamic pricing" : pricingStatusLabels[entry.status]}
        </Badge>
        {price && !entry.price && <p className="mt-1 text-xs font-medium text-muted-foreground">Reference only</p>}
        {dynamic && baseline && (
          <p className="mt-1 max-w-60 text-xs whitespace-normal text-muted-foreground">
            {priceConditions(baseline.conditions)}
          </p>
        )}
        {dynamic && otherConditions && (
          <p className="mt-1 text-xs text-muted-foreground">Other price conditions available</p>
        )}
        {entry.reason && (
          <p
            className="mt-1 line-clamp-2 max-w-52 text-xs whitespace-normal text-muted-foreground"
            title={entry.reason}
          >
            {entry.reason}
          </p>
        )}
      </TableCell>
      {displayedRateFields.map((field) => {
        const flatRate = price ? formatMoney(price[field]) : "Not listed";
        const key = { input_per_million: "p", output_per_million: "c", cache_read_per_million: "cr" }[field];
        const amount = baseline?.rates[key];
        const dynamicRate = amount == null ? "Request-dependent" : formatMoney(amount);
        return (
          <TableCell key={field} className="text-right tabular-nums">
            {dynamic ? dynamicRate : flatRate}
          </TableCell>
        );
      })}
      <TableCell className="min-w-40 text-xs tabular-nums">
        {dynamic && (
          <div className="space-y-1">
            <span>{baseline?.rates.cc == null ? "See prices" : formatMoney(baseline.rates.cc)}</span>
            {price?.cache_write_price_published === false && baseline?.rates.cc != null && (
              <p className="text-muted-foreground">Input-rate fallback</p>
            )}
          </div>
        )}
        {!dynamic &&
          (price ? (
            <dl className="space-y-1">
              {cacheWritePriceRows(
                price.cache_write_price_published === false ? null : price.cache_write_per_million,
                price.cache_write_tiers,
              ).map(({ key, label, amount }) => (
                <div key={key} className="flex justify-between gap-4">
                  <dt>{label}</dt>
                  <dd>{amount === null ? "Not listed" : formatMoney(amount)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <span className="text-muted-foreground">Not listed</span>
          ))}
      </TableCell>
      <TableCell className="sticky right-0 z-sticky-pinned border-l bg-background pr-5 text-right">
        {dynamic && price && (
          <Button variant="outline" size="xs" className="mr-2" onClick={() => onView(price)}>
            View prices
          </Button>
        )}
        <Button variant="outline" size="xs" onClick={() => (entry.price ? onEdit(entry.price) : onChoose(entry))}>
          {rowActionLabel(entry)}
        </Button>
      </TableCell>
    </TableRow>
  );
}

function ReferenceChoice({
  entry,
  onClose,
  onEdit,
}: {
  entry: ModelPriceCatalogEntry | null;
  onClose: () => void;
  onEdit: (price: SalesPrice) => void;
}) {
  const [selected, setSelected] = useState("");
  const choice = entry?.candidates.find((candidate) => candidate.catalog_model === selected);
  return (
    <Dialog
      open={Boolean(entry)}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Choose a price reference</DialogTitle>
          <DialogDescription>
            {entry?.model} does not resolve to one shared default. Select a reference to review and edit before saving a
            site-wide price.
          </DialogDescription>
        </DialogHeader>
        <Select value={selected} onValueChange={(value) => setSelected(value ?? "")}>
          <SelectTrigger aria-label="Price reference" className="w-full">
            <SelectValue placeholder="Select a reference" />
          </SelectTrigger>
          <SelectContent>
            {entry?.candidates.map((candidate) => (
              <SelectItem key={candidate.catalog_model} value={candidate.catalog_model ?? ""}>
                {candidate.catalog_model}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {choice?.expression_policy && (
          <ExpressionPricingRule
            policy={choice.expression_policy}
            display={choice.pricing_display}
            cacheWritePublished={choice.cache_write_price_published}
          />
        )}
        {choice && !choice.expression_policy && (
          <p className="text-sm text-muted-foreground">
            Input {formatMoney(choice.input_per_million)} · Output {formatMoney(choice.output_per_million)} per 1M
            tokens
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!choice}
            onClick={() => {
              if (choice) {
                onEdit(choice);
                onClose();
              }
            }}
          >
            Edit selected reference
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function ModelPriceCatalogTable({
  accessToken,
  enabled,
  onEdit,
  onAdd,
  onImport,
}: {
  accessToken: string | null;
  enabled: boolean;
  onEdit: (price: SalesPrice) => void;
  onAdd: () => void;
  onImport: () => void;
}) {
  const catalog = useQuery({
    queryKey: ["retail", "pricing-catalog"],
    queryFn: () => retailRequest<{ items: ModelPriceCatalogEntry[] }>("/admin/pricing-catalog", accessToken),
    enabled: enabled && Boolean(accessToken),
  });
  const table = useUrlTableState(tableOptions);
  const [choosing, setChoosing] = useState<ModelPriceCatalogEntry | null>(null);
  const [reviewing, setReviewing] = useState<ModelPriceCatalogEntry | null>(null);
  const [viewing, setViewing] = useState<SalesPrice | null>(null);
  const entries = catalog.data?.items ?? [];
  const filterValue = (column: string) =>
    String(table.columnFilters.find((filter) => filter.id === column)?.value ?? "");
  const setFilter = (column: string, value: string) =>
    table.onColumnFiltersChange([
      ...table.columnFilters.filter((filter) => filter.id !== column),
      ...(value ? [{ id: column, value }] : []),
    ]);
  const filters = {
    search: table.search,
    provider: filterValue("provider"),
    mode: filterValue("mode"),
    status: filterValue("status"),
  };
  const direction = table.sorting[0]?.desc ? -1 : 1;
  const filtered = filterModelPrices(entries, filters).toSorted(
    (left, right) => direction * left.model.localeCompare(right.model),
  );
  const page = pricingPage(filtered, table.pagination.pageIndex, table.pagination.pageSize);
  const providers = [...new Set(entries.flatMap((entry) => entry.providers))].sort();
  const modes = [...new Set(entries.flatMap((entry) => entry.modes))].sort();
  const dynamicRules = entries.filter((entry) => entry.status === "expression").length;
  const saved = entries.filter(
    (entry) =>
      entry.status === "custom" ||
      entry.status === "catalog" ||
      (entry.status === "expression" && entry.price?.version != null),
  ).length;
  const choosePrice = (entry: ModelPriceCatalogEntry) => {
    if (hasReviewReferences(entry)) {
      setReviewing(entry);
      return;
    }
    if (entry.candidates.length > 0) setChoosing(entry);
    else {
      const manualPrice = {
        model: entry.model,
        input_per_million: "",
        output_per_million: "",
        cache_read_per_million: "0",
        cache_write_per_million: "0",
      };
      onEdit(manualPrice);
    }
  };

  return (
    <section className="overflow-hidden rounded-xl border" aria-labelledby="base-prices-title">
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
        <h2 id="base-prices-title" className="text-base font-semibold">
          Base prices{" "}
          <Badge variant="secondary" className="ml-2">
            {catalog.isPending ? "Loading..." : `${entries.length} models`}
          </Badge>
        </h2>
        <span className="text-xs text-muted-foreground">
          {saved} saved overrides · {entries.filter((entry) => entry.status === "default").length} built-in defaults ·{" "}
          {dynamicRules} dynamic {dynamicRules === 1 ? "rule" : "rules"}
        </span>
      </div>
      <div className="space-y-4 border-t px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
            Browse the loaded prices for this site&apos;s models and edit any row directly. Saved prices override
            built-in defaults for new requests. Existing bills keep their saved prices.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={catalog.isFetching}
              aria-label="Refresh base prices"
              onClick={() => void catalog.refetch()}
            >
              <RefreshCw className={catalog.isFetching ? "animate-spin" : ""} />
            </Button>
            <Button variant="outline" size="sm" onClick={onImport}>
              <Download />
              Import catalog prices
            </Button>
            <Button variant="outline" size="sm" onClick={onAdd}>
              <Plus />
              Add base price
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-60 flex-1">
            <Search className="absolute top-2.5 left-3 size-4 text-muted-foreground" />
            <Input
              aria-label="Search model prices"
              placeholder="Search model names or providers..."
              value={table.search}
              onChange={(event) => table.setSearch(event.target.value)}
              className="pl-9"
            />
          </div>
          <FilterSelect
            label="Providers"
            value={filterValue("provider")}
            options={providers.map((provider) => ({
              value: provider,
              label: getProviderLogoAndName(provider).displayName,
            }))}
            onChange={(value) => setFilter("provider", value)}
          />
          <FilterSelect
            label="Model types"
            value={filterValue("mode")}
            options={modes.map((mode) => ({ value: mode, label: modeLabel(mode) }))}
            onChange={(value) => setFilter("mode", value)}
          />
          <FilterSelect
            label="Price sources"
            value={filterValue("status")}
            options={Object.entries(pricingStatusLabels).map(([value, label]) => ({ value, label }))}
            onChange={(value) => setFilter("status", value)}
          />
          {(table.search || table.columnFilters.length > 0) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                table.setSearch("");
                table.onColumnFiltersChange([]);
              }}
            >
              Clear filters
            </Button>
          )}
        </div>
      </div>
      {catalog.isPending && (
        <LoadingState title="Loading model prices" description="Reading built-in defaults and saved overrides." />
      )}
      {catalog.error && (
        <StateMessage
          role="alert"
          tone="destructive"
          icon={<CircleAlert />}
          title="Unable to load base prices"
          description={catalog.error.message}
        />
      )}
      {!catalog.isPending && !catalog.error && (
        <>
          <Table aria-label="Model base prices" className="min-w-[1050px] border-t">
            <TableHeader className="bg-muted/40">
              <TableRow>
                <TableHead className="pl-5">Model</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Price source</TableHead>
                <TableHead className="text-right">Input / 1M</TableHead>
                <TableHead className="text-right">Output / 1M</TableHead>
                <TableHead className="text-right">Cache read / 1M</TableHead>
                <TableHead>Cache write / 1M</TableHead>
                <TableHead className="sticky right-0 z-sticky-pinned border-l bg-background pr-5 text-right">
                  Actions
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.items.map((entry) => (
                <PriceRow key={entry.model} entry={entry} onEdit={onEdit} onChoose={choosePrice} onView={setViewing} />
              ))}
            </TableBody>
          </Table>
          {filtered.length === 0 && (
            <StateMessage
              role="status"
              icon={<Tags />}
              title={entries.length ? "No matching model prices" : "No models configured"}
              description={
                entries.length
                  ? "Try another search or clear the filters."
                  : "Configure a model or add a base price to begin."
              }
            />
          )}
          <div className="flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-xs text-muted-foreground">
            <span>
              {filtered.length ? page.start + 1 : 0}–{page.start + page.items.length} of {filtered.length} models · USD
              per 1M tokens
            </span>
            <div className="flex items-center gap-3">
              <Select
                value={String(page.size)}
                onValueChange={(value) => table.onPaginationChange({ pageIndex: 0, pageSize: Number(value) })}
              >
                <SelectTrigger aria-label="Rows per page" size="sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {[25, 50, 100].map((size) => (
                    <SelectItem key={size} value={String(size)}>
                      {size} / page
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span>
                Page {page.index + 1} of {page.pageCount}
              </span>
              <Button
                variant="outline"
                size="icon-sm"
                aria-label="Previous price page"
                disabled={page.index === 0}
                onClick={() => table.onPaginationChange({ pageIndex: page.index - 1, pageSize: page.size })}
              >
                <ChevronLeft />
              </Button>
              <Button
                variant="outline"
                size="icon-sm"
                aria-label="Next price page"
                disabled={page.index + 1 >= page.pageCount}
                onClick={() => table.onPaginationChange({ pageIndex: page.index + 1, pageSize: page.size })}
              >
                <ChevronRight />
              </Button>
            </div>
          </div>
        </>
      )}
      <ReferenceChoice key={choosing?.model} entry={choosing} onClose={() => setChoosing(null)} onEdit={onEdit} />
      <Dialog
        open={Boolean(viewing)}
        onOpenChange={(open) => {
          if (!open) setViewing(null);
        }}
      >
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle>Prices for {viewing?.model}</DialogTitle>
            <DialogDescription>
              Base rates before account discounts. Each row states when a price applies.
            </DialogDescription>
          </DialogHeader>
          {viewing?.expression_policy && (
            <ExpressionPricingRule
              policy={viewing.expression_policy}
              display={viewing.pricing_display}
              cacheWritePublished={viewing.cache_write_price_published}
            />
          )}
        </DialogContent>
      </Dialog>
      <PriceConfigurationReview
        key={reviewing?.model}
        entry={reviewing}
        onClose={() => setReviewing(null)}
        onEdit={onEdit}
      />
    </section>
  );
}
