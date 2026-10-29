"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, CircleAlert, Plus, RefreshCw, Tags } from "lucide-react";
import { FormEvent, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
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
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
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
      toast.success("Model sales price saved");
    },
  });

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
            Model sales prices{" "}
            <Badge variant="secondary" className="ml-2">
              {prices.data?.items.length ?? "..."}
            </Badge>
          </span>
          <ChevronDown className="size-4 text-muted-foreground transition-transform group-open:rotate-180" />
        </summary>
        <div className="border-t">
          <div className="flex flex-wrap items-start justify-between gap-4 px-5 py-4">
            <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">
              Customer base prices are managed independently from channel costs. A pricing plan applies its multiplier
              to these rates. New prices apply to new requests; existing reservations keep their saved prices.
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={prices.isFetching}
                aria-label="Refresh sales prices"
                onClick={() => void prices.refetch()}
              >
                <RefreshCw className={prices.isFetching ? "animate-spin" : ""} />
              </Button>
              <Button variant="outline" size="sm" onClick={() => editPrice(emptyPrice)}>
                <Plus />
                Add sales price
              </Button>
            </div>
          </div>
          {prices.isPending ? (
            <LoadingState title="Loading model prices" description="Retrieving customer base rates." />
          ) : prices.error ? (
            <StateMessage
              role="alert"
              tone="destructive"
              icon={<CircleAlert className="size-5" />}
              title="Unable to load sales prices"
              description={prices.error.message}
            />
          ) : prices.data?.items.length === 0 ? (
            <StateMessage
              role="status"
              icon={<Tags className="size-5" />}
              title="No model sales prices yet"
              description="The first wallet request initializes a model's sales price once from its configured model price. You can add a sales price before that request. Later channel cost changes will not change saved sales prices."
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
            <DialogTitle>Save model sales price</DialogTitle>
            <DialogDescription>Set the customer base rate in USD per one million tokens.</DialogDescription>
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
                <AlertTitle>Sales price was not saved</AlertTitle>
                <AlertDescription>{validation || mutation.error?.message}</AlertDescription>
              </Alert>
            )}
            <p className="text-xs leading-relaxed text-muted-foreground">
              Saving changes the base price for future customer requests. Their assigned pricing plan still applies.
              Requests already reserved keep their original pricing snapshot.
            </p>
            <DialogFooter>
              <Button variant="outline" type="button" onClick={() => setEditing(false)} disabled={mutation.isPending}>
                Cancel
              </Button>
              <Button type="submit" disabled={mutation.isPending}>
                {mutation.isPending ? "Saving..." : "Save sales price"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
