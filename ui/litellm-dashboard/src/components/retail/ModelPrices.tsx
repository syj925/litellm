"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, CircleAlert, Download, Plus, RefreshCw, Tags } from "lucide-react";
import { FormEvent, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
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
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { toast } from "@/lib/toast";
import { formatMoney, retailRequest } from "./api";

interface SalesPrice {
  model: string;
  input_per_million: string;
  output_per_million: string;
  cache_read_per_million: string;
  cache_write_per_million: string;
}

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
  { key: "cache_write_per_million", label: "Cache write" },
] as const;
const emptyPrice: SalesPrice = {
  model: "",
  input_per_million: "",
  output_per_million: "",
  cache_read_per_million: "0",
  cache_write_per_million: "0",
};

export default function ModelPrices({ accessToken, enabled }: { accessToken: string | null; enabled: boolean }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<SalesPrice>(emptyPrice);
  const [validation, setValidation] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [selected, setSelected] = useState<Record<string, { catalog_model: string; source_label: string }>>({});
  const [catalogSearch, setCatalogSearch] = useState("");
  const prices = useQuery({
    queryKey: ["retail", "model-prices"],
    queryFn: () => retailRequest<{ items: SavedSalesPrice[] }>("/admin/model-prices", accessToken),
    enabled: enabled && Boolean(accessToken),
  });
  const mutation = useMutation({
    mutationFn: () =>
      retailRequest<SavedSalesPrice>("/admin/model-prices", accessToken, {
        method: "POST",
        body: JSON.stringify({ ...values, model: values.model.trim() }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["retail", "model-prices"] });
      setEditing(false);
      toast.success("Official reference price saved");
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
      setImporting(false);
      toast.success("Selected reference prices imported");
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
    });
    setValidation(null);
    mutation.reset();
    setEditing(true);
  };

  const savePrice = (event: FormEvent) => {
    event.preventDefault();
    if (!values.model.trim() || !rateFields.every(({ key }) => /^\d{1,9}(?:\.\d{1,9})?$/.test(values[key]))) {
      setValidation("Enter a model name and non-negative decimal USD prices, with no more than 9 decimal places.");
      return;
    }
    setValidation(null);
    mutation.mutate();
  };

  return (
    <>
      <details className="group overflow-hidden rounded-xl border" open>
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-5 py-4 text-sm font-medium">
          <span>
            Official reference prices{" "}
            <Badge variant="secondary" className="ml-2">
              {prices.data?.items.length ?? "..."}
            </Badge>
          </span>
          <ChevronDown className="size-4 text-muted-foreground transition-transform group-open:rotate-180" />
        </summary>
        <div className="border-t">
          <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
            <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">
              Customer charges use these reference rates multiplied by the customer discount. Channel procurement costs
              are separate. Catalog imports require review and confirmation; existing bills keep their saved prices.
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={prices.isFetching}
                aria-label="Refresh reference prices"
                onClick={() => void prices.refetch()}
              >
                <RefreshCw className={prices.isFetching ? "animate-spin" : ""} />
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setSelected({});
                  setCatalogSearch("");
                  importMutation.reset();
                  setImporting(true);
                }}
              >
                <Download />
                Import catalog references
              </Button>
              <Button variant="outline" size="sm" onClick={() => editPrice(emptyPrice)}>
                <Plus />
                Add reference price
              </Button>
            </div>
          </div>
          {prices.isPending ? (
            <LoadingState title="Loading model prices" description="Retrieving reference rates." />
          ) : prices.error ? (
            <StateMessage
              role="alert"
              tone="destructive"
              icon={<CircleAlert className="size-5" />}
              title="Unable to load reference prices"
              description={prices.error.message}
            />
          ) : prices.data?.items.length === 0 ? (
            <StateMessage
              role="status"
              icon={<Tags className="size-5" />}
              title="No reference prices yet"
              description="Import rates from the LiteLLM reference catalog, or enter a manually verified official reference. A configured channel cost is never used as the official reference."
            />
          ) : (
            <Table>
              <TableHeader className="bg-muted/40">
                <TableRow>
                  <TableHead className="pl-5">Model</TableHead>
                  {rateFields.map(({ key, label }) => (
                    <TableHead key={key} className="text-right">
                      {label} / 1M tokens
                    </TableHead>
                  ))}
                  <TableHead>Version</TableHead>
                  <TableHead>Reference source</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="pr-5 text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {prices.data?.items.map((price) => (
                  <TableRow key={price.model}>
                    <TableCell className="pl-5 font-medium">{price.model}</TableCell>
                    {rateFields.map(({ key }) => (
                      <TableCell key={key} className="text-right tabular-nums">
                        {formatMoney(price[key])}
                      </TableCell>
                    ))}
                    <TableCell>
                      <Badge variant="outline">v{price.version}</Badge>
                    </TableCell>
                    <TableCell className="max-w-64 text-xs text-muted-foreground">
                      <Badge variant="outline">
                        {price.source === "catalog"
                          ? "Catalog reference"
                          : price.source === "manual"
                            ? "Manual reference"
                            : "Legacy, unverified"}
                      </Badge>
                      <p className="mt-1 break-words">
                        {price.source_label ??
                          "Existing base price, review before treating it as an official reference"}
                      </p>
                      {price.catalog_model && <p className="mt-1 break-all font-mono">{price.catalog_model}</p>}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {new Date(price.updated_at).toLocaleString("en-US")}
                    </TableCell>
                    <TableCell className="pr-5 text-right">
                      <Button variant="ghost" size="xs" onClick={() => editPrice(price)}>
                        Edit price
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </details>
      <Dialog
        open={editing}
        onOpenChange={(open) => {
          if (!mutation.isPending) setEditing(open);
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Save official reference price</DialogTitle>
            <DialogDescription>
              Enter a manually verified official rate in USD per one million tokens. Customer discounts apply
              separately.
            </DialogDescription>
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
              <div className="grid grid-cols-2 gap-4">
                {rateFields.map(({ key, label }) => (
                  <Field key={key}>
                    <FieldLabel htmlFor={`sales-${key}`}>{label} (USD / 1M)</FieldLabel>
                    <Input
                      id={`sales-${key}`}
                      value={values[key]}
                      onChange={(event) => setValues({ ...values, [key]: event.target.value })}
                      inputMode="decimal"
                      required
                      disabled={mutation.isPending}
                      placeholder="0.00"
                    />
                  </Field>
                ))}
              </div>
            </FieldGroup>
            {(validation || mutation.error) && (
              <Alert variant="error">
                <CircleAlert />
                <AlertTitle>Reference price was not saved</AlertTitle>
                <AlertDescription>{validation || mutation.error?.message}</AlertDescription>
              </Alert>
            )}
            <p className="text-xs leading-relaxed text-muted-foreground">
              Saving changes the reference price for future customer requests. Their effective discount still applies.
              Requests already reserved keep their original pricing snapshot.
            </p>
            <DialogFooter>
              <Button variant="outline" type="button" onClick={() => setEditing(false)} disabled={mutation.isPending}>
                Cancel
              </Button>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "Saving..." : "Save reference price"}
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
            <DialogTitle>Import official reference prices</DialogTitle>
            <DialogDescription>
              Review the LiteLLM reference catalog before applying it. This is a reference catalog, not a live check of
              provider websites. Importing replaces selected models&apos; reference rates for new requests only.
            </DialogDescription>
          </DialogHeader>
          {catalog.isPending ? (
            <LoadingState
              title="Loading reference catalog"
              description="Matching configured public models to catalog rates."
            />
          ) : catalog.error ? (
            <Alert variant="error">
              <AlertTitle>{catalog.error.message}</AlertTitle>
            </Alert>
          ) : (
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
                      <TableHead className="text-right">Cache write / 1M</TableHead>
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
                              aria-label={`Import reference for ${model}`}
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
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {chosen ? formatMoney(chosen.input_per_million) : "Choose reference"}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {chosen ? formatMoney(chosen.output_per_million) : "Choose reference"}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {chosen ? formatMoney(chosen.cache_read_per_million) : "Choose reference"}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {chosen ? formatMoney(chosen.cache_write_per_million) : "Choose reference"}
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
              <AlertTitle>Reference prices were not imported</AlertTitle>
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
              {importMutation.isPending ? "Applying..." : `Apply ${Object.keys(selected).length} reference prices`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
