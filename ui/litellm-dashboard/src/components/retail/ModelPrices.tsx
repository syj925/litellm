"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Plus, Trash2 } from "lucide-react";
import { FormEvent, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
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
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "@/lib/toast";
import { formatMoney, retailRequest } from "./api";
import ModelPriceCatalogTable from "./ModelPriceCatalogTable";
import ExpressionPricingEditor from "./ExpressionPricingEditor";
import ExpressionPricingRule from "./ExpressionPricingRule";
import {
  parseUsageBounds,
  policyFromRates,
  upstreamSearchAllowanceNote,
  type ExpressionPolicy,
} from "./expressionPricing";
import type { SalesPrice } from "./modelPriceCatalog";
import {
  cacheDurationLabel,
  cacheTierEntries,
  cacheWritePriceRows,
  validateCacheTierDrafts,
  type CacheTierDraft,
} from "./cachePricing";

interface SavedSalesPrice extends SalesPrice {
  version: number;
  updated_at: string;
  source?: "legacy" | "catalog" | "manual";
  catalog_model?: string | null;
  source_label?: string;
}

interface ReferenceCandidate extends SalesPrice {
  catalog_model: string;
  source_label: string;
}

interface ReferenceCatalog {
  items: ReferenceCandidate[];
  unavailable: { model: string; reason: string }[];
}

const rateFields = [
  { key: "input_per_million", label: "Input" },
  { key: "output_per_million", label: "Output" },
  { key: "cache_read_per_million", label: "Cache read" },
  { key: "cache_write_per_million", label: "Cache write (default)" },
] as const;
const emptyPrice: SalesPrice = {
  model: "",
  input_per_million: "",
  output_per_million: "",
  cache_read_per_million: "0",
  cache_write_per_million: "0",
};

function referenceRateLabel(
  candidate: ReferenceCandidate | undefined,
  field: (typeof rateFields)[number]["key"],
): string {
  if (!candidate) return "Choose reference";
  return candidate.expression_policy ? "Rule-based" : formatMoney(candidate[field]);
}

export default function ModelPrices({ accessToken, enabled }: { accessToken: string | null; enabled: boolean }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<SalesPrice>(emptyPrice);
  const [expressionEnabled, setExpressionEnabled] = useState(false);
  const [expressionPolicy, setExpressionPolicy] = useState<ExpressionPolicy>(() => policyFromRates(emptyPrice));
  const [hadExpression, setHadExpression] = useState(false);
  const [expressionDraftStarted, setExpressionDraftStarted] = useState(false);
  const [cacheTiers, setCacheTiers] = useState<CacheTierDraft[]>([]);
  const [validation, setValidation] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [selected, setSelected] = useState<Record<string, { catalog_model: string; source_label: string }>>({});
  const [catalogSearch, setCatalogSearch] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      retailRequest<SavedSalesPrice>("/admin/model-prices", accessToken, {
        method: "POST",
        body: JSON.stringify({
          ...values,
          ...(expressionEnabled ? Object.fromEntries(rateFields.map(({ key }) => [key, values[key] || "0"])) : {}),
          model: values.model.trim(),
          cache_write_tiers: validateCacheTierDrafts(cacheTiers).tiers,
          ...(expressionEnabled || hadExpression
            ? { expression_policy: expressionEnabled ? expressionPolicy : null }
            : {}),
        }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail", "model-prices"] });
      void client.invalidateQueries({ queryKey: ["retail", "pricing-catalog"] });
      setEditing(false);
      toast.success("Custom base price saved");
    },
  });
  const catalog = useQuery({
    queryKey: ["retail", "reference-prices"],
    queryFn: () => retailRequest<ReferenceCatalog>("/admin/reference-prices", accessToken),
    enabled: importing && enabled && Boolean(accessToken),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const importMutation = useMutation({
    mutationFn: () =>
      retailRequest("/admin/reference-prices/import", accessToken, {
        method: "POST",
        body: JSON.stringify({
          items: Object.entries(selected).map(([model, choice]) => ({
            model,
            ...choice,
          })),
        }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail", "model-prices"] });
      void client.invalidateQueries({ queryKey: ["retail", "pricing-catalog"] });
      setImporting(false);
      toast.success("Selected base prices imported");
    },
  });
  const groups = [...new Set(catalog.data?.items.map((candidate) => candidate.model) ?? [])].map((model) => ({
    model,
    candidates: catalog.data?.items.filter((candidate) => candidate.model === model) ?? [],
  }));
  const matchingGroups = groups.filter(({ model, candidates }) =>
    `${model} ${candidates.map((candidate) => candidate.catalog_model).join(" ")}`
      .toLowerCase()
      .includes(catalogSearch.toLowerCase()),
  );
  const selectionIsCurrent = Object.entries(selected).every(([model, choice]) =>
    catalog.data?.items.some(
      (candidate) =>
        candidate.model === model &&
        candidate.catalog_model === choice.catalog_model &&
        candidate.source_label === choice.source_label,
    ),
  );

  const editPrice = (price: SalesPrice) => {
    setValues({
      model: price.model,
      input_per_million: price.input_per_million,
      output_per_million: price.output_per_million,
      cache_read_per_million: price.cache_read_per_million,
      cache_write_per_million: price.cache_write_per_million,
      cache_write_price_published: price.cache_write_price_published,
    });
    setCacheTiers(cacheTierEntries(price.cache_write_tiers).map(([ttl_seconds, price]) => ({ ttl_seconds, price })));
    setExpressionEnabled(Boolean(price.expression_policy));
    setHadExpression(Boolean(price.expression_policy));
    setExpressionDraftStarted(Boolean(price.expression_policy));
    setExpressionPolicy(price.expression_policy ?? policyFromRates(price));
    setValidation(null);
    mutation.reset();
    setEditing(true);
  };

  const savePrice = (event: FormEvent) => {
    event.preventDefault();
    if (
      !values.model.trim() ||
      !rateFields.every(({ key }) =>
        /^\d{1,9}(?:\.\d{1,9})?$/.test(expressionEnabled ? values[key] || "0" : values[key]),
      )
    ) {
      setValidation("Enter a model name and non-negative decimal USD prices, with no more than 9 decimal places.");
      return;
    }
    if (
      expressionEnabled &&
      (!expressionPolicy.expression.trim() ||
        !expressionPolicy.timezone.trim() ||
        parseUsageBounds(JSON.stringify(expressionPolicy.usage_bounds)).error)
    ) {
      setValidation("Enter a pricing expression, a rule timezone and valid usage bounds.");
      return;
    }
    const durationPrices = validateCacheTierDrafts(cacheTiers);
    if (durationPrices.error) {
      setValidation(durationPrices.error);
      return;
    }
    setValidation(null);
    mutation.mutate();
  };
  const openImport = () => {
    setSelected({});
    setCatalogSearch("");
    importMutation.reset();
    setImporting(true);
  };
  const showCatalog = !catalog.isPending && !catalog.error;

  return (
    <>
      <ModelPriceCatalogTable
        accessToken={accessToken}
        enabled={enabled}
        onEdit={editPrice}
        onAdd={() => editPrice(emptyPrice)}
        onImport={openImport}
      />
      <Dialog
        open={editing}
        onOpenChange={(open) => {
          if (!mutation.isPending) setEditing(open);
        }}
      >
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Save custom base price</DialogTitle>
            <DialogDescription>
              Configure fixed rates or a dynamic pricing rule. Customer discounts apply separately.
            </DialogDescription>
            <p className="text-xs text-muted-foreground">{upstreamSearchAllowanceNote}</p>
          </DialogHeader>
          <form onSubmit={savePrice} className="flex flex-col gap-5">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="sales-model">Public model name</FieldLabel>
                <Input
                  id="sales-model"
                  value={values.model}
                  onChange={(event) => setValues({ ...values, model: event.target.value })}
                  required
                  maxLength={255}
                  disabled={mutation.isPending}
                  placeholder="Exact model name used by customers"
                />
              </Field>
              <div className="flex gap-2" role="group" aria-label="Pricing method">
                <Button
                  type="button"
                  variant={expressionEnabled ? "outline" : "secondary"}
                  aria-pressed={!expressionEnabled}
                  disabled={mutation.isPending}
                  onClick={() => setExpressionEnabled(false)}
                >
                  Fixed rates
                </Button>
                <Button
                  type="button"
                  variant={expressionEnabled ? "secondary" : "outline"}
                  aria-pressed={expressionEnabled}
                  disabled={mutation.isPending}
                  onClick={() => {
                    if (!expressionDraftStarted) {
                      setExpressionPolicy(
                        policyFromRates({
                          ...values,
                          cache_write_tiers: validateCacheTierDrafts(cacheTiers).tiers ?? {},
                        }),
                      );
                      setExpressionDraftStarted(true);
                    }
                    setExpressionEnabled(true);
                  }}
                >
                  Expression
                </Button>
              </div>
              {expressionEnabled && (
                <>
                  <ExpressionPricingEditor
                    policy={expressionPolicy}
                    onChange={setExpressionPolicy}
                    accessToken={accessToken}
                    disabled={mutation.isPending}
                  />
                </>
              )}
              <div hidden={expressionEnabled} className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  {rateFields.map(({ key, label }) => (
                    <Field key={key}>
                      <FieldLabel htmlFor={`sales-${key}`}>{label} (USD / 1M)</FieldLabel>
                      <Input
                        id={`sales-${key}`}
                        value={values[key]}
                        onChange={(event) =>
                          setValues({
                            ...values,
                            [key]: event.target.value,
                            ...(key === "cache_write_per_million" ? { cache_write_price_published: true } : {}),
                          })
                        }
                        inputMode="decimal"
                        required={!expressionEnabled}
                        disabled={mutation.isPending}
                        placeholder="0.00"
                      />
                      {key === "cache_write_per_million" && values.cache_write_price_published === false && (
                        <p className="text-xs leading-5 text-muted-foreground">
                          This unpublished rate is an internal billing fallback. Editing it publishes a default cache
                          write price.
                        </p>
                      )}
                    </Field>
                  ))}
                </div>
                <section className="space-y-3 rounded-lg border p-4" aria-labelledby="cache-duration-prices-title">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 id="cache-duration-prices-title" className="text-sm font-medium">
                      Cache write duration prices
                    </h3>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={mutation.isPending || cacheTiers.length >= 16}
                      onClick={() => setCacheTiers([...cacheTiers, { ttl_seconds: "", price: "" }])}
                    >
                      <Plus /> Add duration
                    </Button>
                  </div>
                  <p className="text-xs leading-5 text-muted-foreground">
                    Set each write duration separately. These are complete write prices, not a surcharge on input. The
                    duration-specific rate takes precedence over the default cache write rate. Use the default rate for
                    models priced without duration tiers.
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {[300, 1800, 3600, 43200].map((ttl) => (
                      <Button
                        key={ttl}
                        type="button"
                        size="xs"
                        variant="secondary"
                        disabled={
                          mutation.isPending ||
                          cacheTiers.length >= 16 ||
                          cacheTiers.some((tier) => Number(tier.ttl_seconds) === ttl)
                        }
                        onClick={() => setCacheTiers([...cacheTiers, { ttl_seconds: String(ttl), price: "" }])}
                      >
                        + {cacheDurationLabel(ttl)}
                      </Button>
                    ))}
                  </div>
                  {cacheTiers.map((tier, index) => (
                    <div key={index} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-end gap-3">
                      <Field>
                        <FieldLabel htmlFor={`cache-ttl-${index}`}>Duration {index + 1} (seconds)</FieldLabel>
                        <Input
                          id={`cache-ttl-${index}`}
                          inputMode="numeric"
                          value={tier.ttl_seconds}
                          disabled={mutation.isPending}
                          onChange={(event) =>
                            setCacheTiers(
                              cacheTiers.map((row, position) =>
                                position === index ? { ...row, ttl_seconds: event.target.value } : row,
                              ),
                            )
                          }
                          placeholder="300"
                        />
                      </Field>
                      <Field>
                        <FieldLabel htmlFor={`cache-tier-price-${index}`}>
                          {tier.ttl_seconds ? cacheDurationLabel(tier.ttl_seconds) : "Write"} price (USD / 1M)
                        </FieldLabel>
                        <Input
                          id={`cache-tier-price-${index}`}
                          inputMode="decimal"
                          value={tier.price}
                          disabled={mutation.isPending}
                          onChange={(event) =>
                            setCacheTiers(
                              cacheTiers.map((row, position) =>
                                position === index ? { ...row, price: event.target.value } : row,
                              ),
                            )
                          }
                          placeholder="0.00"
                        />
                      </Field>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        aria-label={`Remove cache duration ${index + 1}`}
                        disabled={mutation.isPending}
                        onClick={() => setCacheTiers(cacheTiers.filter((_, position) => position !== index))}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  ))}
                  {!cacheTiers.length && (
                    <p className="text-xs text-muted-foreground">
                      No duration prices configured. Add the durations supported by this model.
                    </p>
                  )}
                </section>
              </div>
            </FieldGroup>
            {(validation || mutation.error) && (
              <Alert variant="error">
                <CircleAlert />
                <AlertTitle>Base price was not saved</AlertTitle>
                <AlertDescription>{validation || mutation.error?.message}</AlertDescription>
              </Alert>
            )}
            <p className="text-xs leading-relaxed text-muted-foreground">
              Saving changes the base price for future customer requests. Their effective discount still applies.
              Requests already reserved keep their original pricing snapshot.
            </p>
            <DialogFooter>
              <Button variant="outline" type="button" onClick={() => setEditing(false)} disabled={mutation.isPending}>
                Cancel
              </Button>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "Saving..." : "Save base price"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog
        open={importing}
        onOpenChange={(open) => {
          if (!importMutation.isPending) setImporting(open);
        }}
      >
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle>Import catalog prices</DialogTitle>
            <DialogDescription>
              Review the LiteLLM reference catalog before applying it. This is a reference catalog, not a live check of
              provider websites. Importing replaces selected models&apos; base rates for new requests only.
            </DialogDescription>
            <p className="text-xs text-muted-foreground">{upstreamSearchAllowanceNote}</p>
          </DialogHeader>
          {catalog.isPending && (
            <LoadingState
              title="Loading reference catalog"
              description="Matching configured public models to catalog rates."
            />
          )}
          {!catalog.isPending && catalog.error && (
            <Alert variant="error">
              <AlertTitle>{catalog.error.message}</AlertTitle>
            </Alert>
          )}
          {showCatalog && (
            <>
              <div className="flex flex-wrap gap-2">
                <Input
                  aria-label="Search reference models"
                  placeholder="Search public or catalog model..."
                  className="min-w-60 flex-1"
                  value={catalogSearch}
                  onChange={(event) => setCatalogSearch(event.target.value)}
                />
                <Button
                  variant="outline"
                  size="sm"
                  disabled={importMutation.isPending}
                  onClick={() =>
                    setSelected({
                      ...selected,
                      ...Object.fromEntries(
                        matchingGroups
                          .filter(({ candidates }) => candidates.length === 1)
                          .map(({ model, candidates }) => [
                            model,
                            { catalog_model: candidates[0].catalog_model, source_label: candidates[0].source_label },
                          ]),
                      ),
                    })
                  }
                >
                  Select single-match models
                </Button>
                <Button variant="ghost" size="sm" disabled={importMutation.isPending} onClick={() => setSelected({})}>
                  Clear selection
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={importMutation.isPending || catalog.isFetching}
                  onClick={() => {
                    setSelected({});
                    void catalog.refetch();
                  }}
                >
                  Refresh catalog
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Models with several catalog matches require an explicit choice. Channel cost overrides are excluded.
              </p>
              <div className="max-h-80 overflow-auto rounded-lg border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-12">Select</TableHead>
                      <TableHead>Public model / Catalog reference</TableHead>
                      <TableHead className="text-right">Input / 1M</TableHead>
                      <TableHead className="text-right">Output / 1M</TableHead>
                      <TableHead className="text-right">Cache read / 1M</TableHead>
                      <TableHead className="text-right">Cache write prices / 1M</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {matchingGroups.map(({ model, candidates }) => {
                      const chosen =
                        candidates.find((candidate) => candidate.catalog_model === selected[model]?.catalog_model) ??
                        (candidates.length === 1 ? candidates[0] : undefined);
                      return (
                        <TableRow key={model}>
                          <TableCell>
                            <Checkbox
                              aria-label={`Import base price for ${model}`}
                              checked={Boolean(selected[model])}
                              disabled={importMutation.isPending || !chosen}
                              onCheckedChange={(checked) =>
                                setSelected(
                                  checked && chosen
                                    ? {
                                        ...selected,
                                        [model]: {
                                          catalog_model: chosen.catalog_model,
                                          source_label: chosen.source_label,
                                        },
                                      }
                                    : Object.fromEntries(Object.entries(selected).filter(([name]) => name !== model)),
                                )
                              }
                            />
                          </TableCell>
                          <TableCell className="min-w-64 max-w-80 whitespace-normal">
                            <p className="mb-1 max-w-72 break-all text-sm font-medium">{model}</p>
                            {candidates.length === 1 ? (
                              <p className="max-w-72 break-all text-xs text-muted-foreground">
                                {candidates[0].catalog_model}
                              </p>
                            ) : (
                              <Select
                                items={candidates.map((candidate) => ({
                                  value: candidate.catalog_model,
                                  label: candidate.catalog_model,
                                }))}
                                value={selected[model]?.catalog_model ?? null}
                                onValueChange={(value) => {
                                  const candidate = candidates.find((item) => item.catalog_model === value);
                                  if (candidate)
                                    setSelected({
                                      ...selected,
                                      [model]: {
                                        catalog_model: candidate.catalog_model,
                                        source_label: candidate.source_label,
                                      },
                                    });
                                }}
                                disabled={importMutation.isPending}
                              >
                                <SelectTrigger aria-label={`Catalog reference for ${model}`} className="w-full">
                                  <SelectValue placeholder="Choose one catalog reference" />
                                </SelectTrigger>
                                <SelectContent>
                                  {candidates.map((candidate) => (
                                    <SelectItem key={candidate.catalog_model} value={candidate.catalog_model}>
                                      {candidate.catalog_model}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            )}
                            <p className="mt-1 max-w-72 break-all text-xs text-muted-foreground">
                              {chosen?.source_label}
                            </p>
                            {chosen?.expression_policy && (
                              <details className="mt-2">
                                <summary className="cursor-pointer text-xs font-medium">Review dynamic rule</summary>
                                <ExpressionPricingRule policy={chosen.expression_policy} />
                              </details>
                            )}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {referenceRateLabel(chosen, "input_per_million")}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {referenceRateLabel(chosen, "output_per_million")}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {referenceRateLabel(chosen, "cache_read_per_million")}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {chosen?.expression_policy && "See dynamic rule"}
                            {!chosen && "Choose reference"}
                            {chosen && !chosen.expression_policy && (
                              <div className="space-y-1">
                                {cacheWritePriceRows(
                                  chosen.cache_write_price_published === false ? null : chosen.cache_write_per_million,
                                  chosen.cache_write_tiers,
                                ).map(({ key, label, amount }) => (
                                  <p key={key}>
                                    {label}: {amount === null ? "Not listed" : formatMoney(amount)}
                                  </p>
                                ))}
                              </div>
                            )}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                    {matchingGroups.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                          No matching catalog references
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </div>
              {catalog.data?.unavailable.length ? (
                <details className="rounded-lg border p-3 text-sm">
                  <summary className="cursor-pointer">
                    {catalog.data.unavailable.length} models need a manual reference
                  </summary>
                  <ul className="mt-3 space-y-2 text-xs text-muted-foreground">
                    {catalog.data.unavailable.map(({ model, reason }) => (
                      <li key={model}>
                        <span className="font-medium">{model}</span>: {reason}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </>
          )}
          {importMutation.error && (
            <Alert variant="error">
              <AlertTitle>Base prices were not imported</AlertTitle>
              <AlertDescription>{importMutation.error.message}</AlertDescription>
            </Alert>
          )}
          {!selectionIsCurrent && (
            <Alert variant="error">
              <AlertTitle>
                The reference catalog changed. Refresh the catalog and review your selection again.
              </AlertTitle>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" disabled={importMutation.isPending} onClick={() => setImporting(false)}>
              Cancel
            </Button>
            <Button
              disabled={
                Object.keys(selected).length === 0 ||
                importMutation.isPending ||
                catalog.isFetching ||
                Boolean(catalog.error) ||
                !selectionIsCurrent
              }
              onClick={() => importMutation.mutate()}
            >
              {importMutation.isPending ? "Applying..." : `Apply ${Object.keys(selected).length} base prices`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
