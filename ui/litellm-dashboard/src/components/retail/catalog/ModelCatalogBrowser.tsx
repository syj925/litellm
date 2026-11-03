"use client";

import { useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useQueryStates } from "nuqs";
import { ArrowRight, Box, Columns3, Filter, List, RefreshCw, Search, Table2, X } from "lucide-react";
import { getProxyBaseUrl } from "@/components/networking";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { uiHref } from "@/utils/uiHref";
import { retailRequest } from "../api";
import CatalogFilters, { CatalogSelect } from "./CatalogFilters";
import CatalogModelList from "./CatalogModelList";
import CatalogModelDetail from "./CatalogModelDetail";
import CatalogCompare from "./CatalogCompare";
import ModelBenchmarkExplorer from "./ModelBenchmarkExplorer";
import { catalogParsers, filterCatalog, type CatalogFilters as Filters } from "./catalogState";
import type { CatalogResponse } from "./types";
import styles from "./PublicModelCatalog.module.css";
import { publicSiteThemeClassName } from "../public-site/PublicSiteShell";

const sortOptions = [
  { value: "name", label: "Name A–Z" },
  { value: "context", label: "Context: largest" },
  { value: "input_price", label: "Input price: lowest" },
  { value: "output_price", label: "Output price: lowest" },
] as const;
const subscribeToOrigin = () => () => {};
const browserOrigin = () => window.location.origin;
const serverOrigin = () => "";
const publicTitles = { models: "Models", pricing: "Pricing" } as const;
const pricingParsers = {
  ...catalogParsers,
  view: catalogParsers.view.withDefault("table"),
  model_tab: catalogParsers.model_tab.withDefault("pricing"),
};

export default function ModelCatalogBrowser({
  publicView = false,
  accessToken = null,
  userId = null,
  publicMode = "models",
}: {
  publicView?: boolean;
  accessToken?: string | null;
  userId?: string | null;
  publicMode?: "models" | "pricing";
}) {
  const pricingView = publicView && publicMode === "pricing";
  const [filters, setFilters] = useQueryStates(pricingView ? pricingParsers : catalogParsers);
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);
  const [comparisonOpen, setComparisonOpen] = useState(false);
  const origin = useSyncExternalStore(subscribeToOrigin, browserOrigin, serverOrigin);
  const apiBaseUrl = origin ? new URL(getProxyBaseUrl() || "/", origin).href.replace(/\/$/, "") : "";
  const catalog = useQuery({
    queryKey: ["retail", "model-catalog", publicView ? "public" : userId],
    queryFn: () =>
      retailRequest<CatalogResponse>(
        publicView ? "/public/model-catalog" : "/model-catalog",
        publicView ? null : accessToken,
      ),
    enabled: publicView || Boolean(accessToken),
    retry: false,
    staleTime: 60_000,
  });
  const models = catalog.data?.items ?? [];
  const priceBasis = publicView ? "reference" : filters.price;
  const visible = filterCatalog(models, {
    ...filters,
    price: priceBasis,
    availability: publicView ? "all" : filters.availability,
  });
  const current = models.find((model) => model.id === filters.model);
  const compared = models.filter((model) => filters.compare.includes(model.id)).slice(0, 3);
  const modes = [...new Set(models.flatMap((model) => (model.mode ? [model.mode] : [])))].sort();
  const activeFilters =
    filters.authors.length +
    filters.pools.length +
    filters.capabilities.length +
    filters.input.length +
    Number(filters.context > 0) +
    Number(filters.max_price >= 0) +
    Number(filters.availability !== "all") +
    Number(filters.mode !== "all");
  const updateFilters = (value: Partial<Filters>) => void setFilters(value);
  const resetFilters = () =>
    void setFilters({
      q: null,
      authors: null,
      pools: null,
      capabilities: null,
      input: null,
      context: null,
      max_price: null,
      availability: null,
      mode: null,
    });
  const openModel = (id: string) => void setFilters({ model: id, model_tab: null }, { history: "push" });
  const toggleComparison = (id: string) => {
    const selected = compared.map((model) => model.id);
    void setFilters({
      compare: selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id].slice(0, 3),
    });
  };
  if (catalog.isPending)
    return (
      <LoadingState
        title={pricingView ? "Loading pricing" : "Loading model catalog"}
        description={
          publicView ? "Loading published models and reference prices." : "Loading your available models and resources."
        }
      />
    );
  if (catalog.error)
    return (
      <div className="p-6">
        <Alert variant="error">
          <AlertTitle>Unable to load model catalog</AlertTitle>
          <AlertDescription>{catalog.error.message}</AlertDescription>
        </Alert>
        <Button className="mt-4" variant="outline" onClick={() => void catalog.refetch()}>
          Try again
        </Button>
      </div>
    );
  if (filters.model && current)
    return (
      <div className={publicView ? styles.browser : "min-w-0 p-5 lg:p-7"}>
        <CatalogModelDetail
          model={current}
          activeTab={filters.model_tab}
          onTabChange={(tab) => void setFilters({ model_tab: tab })}
          onBack={() => void setFilters({ model: null, model_tab: null })}
          apiBaseUrl={apiBaseUrl}
          publicView={publicView}
          accessToken={accessToken}
        />
      </div>
    );
  if (filters.model && !current)
    return (
      <div className="p-6">
        <Alert>
          <AlertTitle>Model not found</AlertTitle>
          <AlertDescription>This model is no longer published or is not visible to this account.</AlertDescription>
        </Alert>
        <Button className="mt-4" variant="outline" onClick={() => void setFilters({ model: null })}>
          Back to models
        </Button>
      </div>
    );
  return (
    <div className={publicView ? styles.browser : "min-w-0 p-5 lg:p-7"}>
      <div
        className={["mb-4 flex flex-wrap items-start justify-between gap-4", publicView ? styles.heading : ""].join(
          " ",
        )}
      >
        <div>
          <h1 className={publicView ? styles.title : "text-2xl font-semibold tracking-tight"}>
            {publicView ? publicTitles[publicMode] : "My models"}
          </h1>
          {pricingView && (
            <p className={styles.description}>
              Published reference rates in USD per million tokens. Open a model for cache pricing, tool fees and
              applicable usage conditions. Sign in to see your account rates.
            </p>
          )}
          {publicView && !pricingView && (
            <p className={styles.description}>
              Explore models, compare capabilities, and choose an API format for your application.
            </p>
          )}
          {!publicView && (
            <p className="mt-1.5 text-sm text-muted-foreground">
              Your available models, resource pools and account prices.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {!publicView && (
            <Link href={uiHref("models")} className="text-sm font-medium hover:underline">
              Explore all models
            </Link>
          )}
          <Button variant="outline" disabled={compared.length < 2} onClick={() => setComparisonOpen(true)}>
            <Columns3 />
            Compare{compared.length > 0 ? ` (${compared.length})` : ""}
          </Button>
          <ModelBenchmarkExplorer models={visible} publicView={publicView} />
          <Button
            variant="outline"
            size="icon"
            aria-label="Refresh model catalog"
            disabled={catalog.isFetching}
            onClick={() => void catalog.refetch()}
          >
            <RefreshCw className={catalog.isFetching ? "animate-spin" : ""} />
          </Button>
        </div>
      </div>
      <div className="grid min-w-0 gap-7 lg:grid-cols-[180px_minmax(0,1fr)] xl:grid-cols-[200px_minmax(0,1fr)]">
        <aside aria-label="Model filters" className="hidden border-r pr-5 lg:block">
          <div className="mb-5 flex items-center justify-between">
            <p className="text-sm font-semibold">Filters{activeFilters > 0 ? ` (${activeFilters})` : ""}</p>
            {activeFilters > 0 && (
              <button className="text-xs text-muted-foreground hover:underline" onClick={resetFilters}>
                Reset
              </button>
            )}
          </div>
          <CatalogFilters
            publicView={publicView}
            prefix="sidebar"
            models={models}
            filters={filters}
            onChange={updateFilters}
          />
        </aside>
        <section className="min-w-0" aria-label="Model catalog">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-44 flex-1">
              <Search
                aria-hidden="true"
                className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground"
              />
              <Input
                aria-label="Search models"
                placeholder="Search models, authors or resources…"
                value={filters.q}
                onChange={(event) => void setFilters({ q: event.target.value })}
                className="h-9 pl-9"
              />
            </div>
            <div className="w-44">
              <CatalogSelect
                publicView={publicView}
                label="Sort models"
                value={filters.sort}
                options={[...sortOptions]}
                onChange={(value) => {
                  const option = sortOptions.find((item) => item.value === value);
                  if (option) void setFilters({ sort: option.value });
                }}
              />
            </div>
            <div className="flex rounded-md border p-0.5" role="group" aria-label="Display format">
              <Button
                variant={filters.view === "list" ? "secondary" : "ghost"}
                size="icon-sm"
                aria-label="List view"
                aria-pressed={filters.view === "list"}
                onClick={() => void setFilters({ view: "list" })}
              >
                <List />
              </Button>
              <Button
                variant={filters.view === "table" ? "secondary" : "ghost"}
                size="icon-sm"
                aria-label="Table view"
                aria-pressed={filters.view === "table"}
                onClick={() => void setFilters({ view: "table" })}
              >
                <Table2 />
              </Button>
            </div>
            <Button variant="outline" className="lg:hidden" onClick={() => setMobileFiltersOpen(true)}>
              <Filter />
              Filters{activeFilters > 0 ? ` (${activeFilters})` : ""}
            </Button>
          </div>
          {!publicView && (
            <div className="my-4 flex flex-wrap items-center justify-between gap-3 border-b pb-3">
              <p className="text-sm font-medium">
                {publicView ? "All published models" : "Models available to you"}{" "}
                <span className="ml-1 text-xs text-muted-foreground">{models.length}</span>
              </p>
              {!publicView && (
                <div className="w-40">
                  <CatalogSelect
                    publicView={publicView}
                    label="Pricing display"
                    value={filters.price}
                    options={[
                      { value: "reference", label: "Reference prices" },
                      { value: "customer", label: "Your prices" },
                    ]}
                    onChange={(value) => {
                      if (value === "reference" || value === "customer") void setFilters({ price: value });
                    }}
                  />
                </div>
              )}
            </div>
          )}
          {modes.length > 1 && (
            <div className="my-3 flex flex-wrap gap-1.5 border-b pb-2" role="group" aria-label="Model type">
              {["all", ...modes].map((mode) => (
                <Button
                  key={mode}
                  variant={filters.mode === mode ? "secondary" : "ghost"}
                  size="sm"
                  aria-pressed={filters.mode === mode}
                  onClick={() => void setFilters({ mode })}
                >
                  {mode === "all"
                    ? "All types"
                    : mode.replaceAll("_", " ").replace(/(^|\s)\S/g, (letter) => letter.toUpperCase())}
                </Button>
              ))}
            </div>
          )}
          <div className="my-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <p aria-live="polite">
              {visible.length} {visible.length === 1 ? "model" : "models"}
              {filters.q ? ` matching “${filters.q}”` : ""}
            </p>
            <span>Prices per 1M tokens · USD</span>
          </div>
          {visible.length ? (
            <CatalogModelList
              models={visible}
              basis={priceBasis}
              publicView={publicView}
              view={filters.view}
              selectedIds={compared.map((model) => model.id)}
              onToggle={toggleComparison}
              onOpen={openModel}
            />
          ) : (
            <div className="rounded-lg border border-dashed py-16 text-center">
              <Box className="mx-auto mb-3 size-7 text-muted-foreground" />
              <h2 className="font-medium">No models match your filters</h2>
              <p className="mt-2 text-sm text-muted-foreground">Try a different search or reset the filters.</p>
              <Button variant="outline" className="mt-4" onClick={resetFilters}>
                Reset filters
              </Button>
            </div>
          )}
          <p className="mt-5 text-xs leading-5 text-muted-foreground">
            {catalog.data?.metadata_note}{" "}
            {publicView
              ? "Sign in to see your resource access and account prices. "
              : "Your keys can further restrict these resources. "}
            <Link
              href={uiHref(publicView ? "login" : "api-keys")}
              className="font-medium text-foreground hover:underline"
            >
              {publicView ? "Sign in to use models" : "Manage your keys"} <ArrowRight className="inline size-3" />
            </Link>
          </p>
        </section>
      </div>
      {compared.length > 0 && (
        <div className="sticky bottom-3 z-chrome mx-auto mt-5 flex w-fit max-w-full flex-wrap items-center justify-center gap-3 rounded-lg border bg-background px-4 py-2.5 shadow-md">
          <span className="text-sm">{compared.length} of 3 models selected</span>
          <Button size="sm" disabled={compared.length < 2} onClick={() => setComparisonOpen(true)}>
            Compare models
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Clear comparison"
            onClick={() => void setFilters({ compare: null })}
          >
            <X />
          </Button>
        </div>
      )}
      <Dialog open={mobileFiltersOpen} onOpenChange={setMobileFiltersOpen}>
        <DialogContent
          className={["max-h-[85vh] overflow-y-auto sm:max-w-md", publicView ? publicSiteThemeClassName : ""].join(" ")}
        >
          <DialogHeader>
            <DialogTitle>Filter models</DialogTitle>
          </DialogHeader>
          <CatalogFilters
            publicView={publicView}
            prefix="mobile"
            models={models}
            filters={filters}
            onChange={updateFilters}
          />
          <Button onClick={() => setMobileFiltersOpen(false)}>Show {visible.length} models</Button>
        </DialogContent>
      </Dialog>
      <CatalogCompare
        models={compared}
        basis={priceBasis}
        publicView={publicView}
        open={comparisonOpen}
        onOpenChange={setComparisonOpen}
      />
    </div>
  );
}
