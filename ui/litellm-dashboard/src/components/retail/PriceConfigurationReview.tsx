"use client";

import { useState } from "react";
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { ModelPriceCatalogEntry, SalesPrice } from "./modelPriceCatalog";
import { policyFromRates } from "./expressionPricing";

export default function PriceConfigurationReview({
  entry,
  onClose,
  onEdit,
}: {
  entry: ModelPriceCatalogEntry | null;
  onClose: () => void;
  onEdit: (price: SalesPrice) => void;
}) {
  const references = entry?.review_references ?? [];
  const [selected, setSelected] = useState(references.length === 1 ? references[0].catalog_model : "");
  const choice = references.find((reference) => reference.catalog_model === selected);
  const useReference = () => {
    if (!choice?.base_price || !entry) return;
    onEdit({ ...choice.base_price, model: entry.model });
    onClose();
  };
  const setTokenPrice = () => {
    if (!entry) return;
    const manualPrice = {
      model: entry.model,
      input_per_million: "",
      output_per_million: "",
      cache_read_per_million: "0",
      cache_write_per_million: "0",
    };
    onEdit(manualPrice);
    onClose();
  };
  const setDynamicPrice = () => {
    if (!entry || !choice) return;
    const base = choice.base_price ?? {
      model: entry.model,
      input_per_million: "0",
      output_per_million: "0",
      cache_read_per_million: "0",
      cache_write_per_million: "0",
    };
    onEdit({ ...base, model: entry.model, expression_policy: choice.expression_policy ?? policyFromRates(base) });
    onClose();
  };
  return (
    <Dialog
      open={Boolean(entry)}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Review price configuration</DialogTitle>
          <DialogDescription>
            {entry?.model}: these are the loaded reference settings. Context tiers and non-token prices are not
            automatically used as a flat site-wide token price. Review before creating a custom base price.
          </DialogDescription>
        </DialogHeader>
        {references.length > 1 && (
          <Select value={selected} onValueChange={(value) => setSelected(value ?? "")}>
            <SelectTrigger aria-label="Configuration reference" className="w-full">
              <SelectValue placeholder="Choose a configuration" />
            </SelectTrigger>
            <SelectContent>
              {references.map((reference) => (
                <SelectItem key={reference.catalog_model} value={reference.catalog_model}>
                  {reference.catalog_model}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {choice && (
          <>
            <div className="space-y-1 text-xs text-muted-foreground">
              <p className="break-all font-mono text-foreground">{choice.catalog_model}</p>
              <p>{choice.source_label}</p>
              <Badge variant="outline">Reference only</Badge>
            </div>
            <Table aria-label="Internal price configuration">
              <TableHeader>
                <TableRow>
                  <TableHead>Configuration field</TableHead>
                  <TableHead className="text-right">Configured rate (USD)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(choice.rates).map(([field, amount]) => (
                  <TableRow key={field}>
                    <TableCell className="max-w-96 font-mono text-xs break-all whitespace-normal">{field}</TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">{amount}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <p className="text-xs leading-relaxed text-muted-foreground">
              Units follow the configuration field name. A per_token value is USD per token, not USD per million tokens.
              Threshold fields apply only to their named context range.
            </p>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {choice.expression_policy
                ? "Configure dynamic pricing preserves the supported context, cache and service conditions in an editable formula. Review and simulate it before saving."
                : "This reference cannot be fully translated into an expression. Configure dynamic pricing opens a base-rate draft; add the missing conditions and units before saving. The raw settings above remain the reference."}
            </p>
            {choice.base_price ? (
              <p className="text-sm leading-relaxed text-muted-foreground">
                Use the base token rates to start an editable custom price. Additional context tiers are not copied into
                a flat price. Nothing changes until you save the next form.
              </p>
            ) : (
              <p className="text-sm leading-relaxed text-muted-foreground">
                This configuration uses non-token or unsupported pricing units. Its rates cannot be copied into the
                token price editor. You can explicitly configure a separate custom token price.
              </p>
            )}
          </>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="outline" disabled={!choice} onClick={setDynamicPrice}>
            Configure dynamic pricing
          </Button>
          {choice?.base_price ? (
            <Button onClick={useReference}>Use as custom base price</Button>
          ) : (
            <Button disabled={!choice} onClick={setTokenPrice}>
              Set custom token price
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
