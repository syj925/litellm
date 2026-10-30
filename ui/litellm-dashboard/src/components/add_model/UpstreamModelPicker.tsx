"use client";

import { ChevronLeft, ChevronRight, Download, Loader2, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useFormContext, useWatch } from "react-hook-form";
import type { MountedFormValues } from "@/components/common_components/MountedFormField";
import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "@/components/networking";
import { Providers, provider_map } from "@/components/provider_info_helpers";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
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
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { filterModelIds, mergeUpstreamModels, uniqueModelIds } from "./upstreamModelSelection";

interface DiscoveryResponse {
  models: string[];
  protocol: string;
  source: string;
  count: number;
  truncated: boolean;
}

interface UpstreamModelPickerProps {
  selectedProvider: string | null;
  accessToken: string | null;
  canDiscover: boolean;
  onModelsDiscovered?: (models: string[]) => void;
}

const textValue = (value: unknown): string => (typeof value === "string" ? value.trim() : "");

function errorMessage(value: unknown, status: number): string {
  if (typeof value === "object" && value !== null && "detail" in value && typeof value.detail === "string") {
    return value.detail;
  }
  return `Unable to fetch upstream models (${status}). Check the provider, API base, and credentials.`;
}

function discoveryResponse(value: unknown): DiscoveryResponse {
  if (typeof value !== "object" || value === null || !("models" in value) || !Array.isArray(value.models)) {
    throw new Error("The gateway returned an invalid model list.");
  }
  const models = uniqueModelIds(value.models.filter((model): model is string => typeof model === "string"));
  return {
    models,
    count: models.length,
    protocol: "protocol" in value && typeof value.protocol === "string" ? value.protocol : "",
    source: "source" in value && typeof value.source === "string" ? value.source : "",
    truncated: "truncated" in value && value.truncated === true,
  };
}

export default function UpstreamModelPicker({
  selectedProvider,
  accessToken,
  canDiscover,
  onModelsDiscovered,
}: UpstreamModelPickerProps) {
  const form = useFormContext<MountedFormValues>();
  const [baseValue, keyValue, geminiKeyValue, credentialValue] = useWatch({
    control: form.control,
    name: ["api_base", "api_key", "gemini_api_key", "litellm_credential_name"],
  });
  const apiBase = textValue(baseValue);
  const apiKey = textValue(keyValue) || textValue(geminiKeyValue);
  const credentialName = textValue(credentialValue);
  const provider = selectedProvider ? provider_map[selectedProvider] ?? selectedProvider.toLowerCase() : "";
  const unsupported = /^(azure|bedrock|sagemaker|vertex|palm|oci|watsonx)/.test(provider);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<DiscoveryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const generation = useRef(0);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    generation.current += 1;
    request.current?.abort();
    setOpen(false);
    setLoading(false);
    setResult(null);
    setSelected([]);
    setError(null);
    setQuery("");
    setPage(1);
    onModelsDiscovered?.([]);
    return () => request.current?.abort();
  }, [selectedProvider, baseValue, keyValue, geminiKeyValue, credentialValue, onModelsDiscovered]);

  const fetchModels = async () => {
    request.current?.abort();
    const current = ++generation.current;
    const controller = new AbortController();
    request.current = controller;
    setOpen(true);
    setLoading(true);
    setResult(null);
    setSelected([]);
    setQuery("");
    setPage(1);
    setError(null);
    try {
      const response = await fetch(`${getProxyBaseUrl()}/retail/admin/model-discovery`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          [getGlobalLitellmHeaderName()]: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
          provider,
          ...(credentialName
            ? { credential_name: credentialName }
            : { ...(apiBase ? { upstream_api_base: apiBase } : {}), ...(apiKey ? { upstream_api_key: apiKey } : {}) }),
        }),
        credentials: "same-origin",
        cache: "no-store",
      });
      const body: unknown = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      const next = discoveryResponse(body);
      if (generation.current === current) {
        setResult(next);
        onModelsDiscovered?.(next.models.map((model) => `${provider}/${model}`));
      }
    } catch (failure) {
      if (generation.current === current && !controller.signal.aborted) {
        setError(failure instanceof Error ? failure.message : "Unable to fetch upstream models.");
      }
    } finally {
      if (generation.current === current) setLoading(false);
    }
  };

  const changeOpen = (next: boolean) => {
    setOpen(next);
    if (!next) {
      generation.current += 1;
      request.current?.abort();
      setLoading(false);
    }
  };

  const importSelected = () => {
    const values = mergeUpstreamModels(form.getValues("model"), form.getValues("model_mappings"), selected, provider);
    form.setValue("model_name", undefined, { shouldDirty: true });
    form.setValue("model_mappings", values.model_mappings, { shouldDirty: true, shouldValidate: true });
    form.setValue("model", values.model, { shouldDirty: true, shouldValidate: true });
    changeOpen(false);
  };

  if (!canDiscover) return null;
  const filtered = filterModelIds(result?.models ?? [], query);
  const pageCount = Math.max(1, Math.ceil(filtered.length / 100));
  const currentPage = Math.min(page, pageCount);
  const pageModels = filtered.slice((currentPage - 1) * 100, currentPage * 100);
  const selectedSet = new Set(selected);
  const credentialsReady = apiKey.length > 0 || credentialName.length > 0 || provider.startsWith("ollama");
  const baseReady =
    credentialName.length > 0 ||
    apiBase.length > 0 ||
    !["OpenAI_Compatible", Providers.OpenAI_Compatible].includes(selectedProvider ?? "");

  return (
    <div className="mb-4 flex flex-col items-start gap-2">
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => void fetchModels()}
        disabled={!provider || unsupported || !credentialsReady || !baseReady || !accessToken}
      >
        <Download />
        Fetch upstream models
      </Button>
      <p className="text-xs text-muted-foreground">
        {unsupported
          ? "This provider uses account or deployment discovery. Select models from the provider catalog or enter the deployment name."
          : "Enter the API base and credentials below, then fetch the upstream list to select and add models in bulk."}
      </p>
      <Dialog open={open} onOpenChange={changeOpen}>
        <DialogContent className="flex max-h-[85dvh] flex-col sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Import upstream models</DialogTitle>
            <DialogDescription>
              Select models for this connection. Importing fills the model mappings; use Add Model to save them.
            </DialogDescription>
          </DialogHeader>
          {loading && (
            <LoadingState
              title="Fetching upstream models"
              description="Reading the models available to these credentials."
            />
          )}
          {error && (
            <Alert variant="error">
              <AlertTitle>Unable to fetch models</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          {result && (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative min-w-48 flex-1">
                  <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
                  <Input
                    aria-label="Search upstream models"
                    placeholder="Search model names..."
                    value={query}
                    onChange={(event) => {
                      setQuery(event.target.value);
                      setPage(1);
                    }}
                    className="pl-9"
                  />
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={filtered.length === 0}
                  onClick={() => setSelected(uniqueModelIds([...selected, ...filtered]))}
                >
                  Select all results
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={selected.length === 0}
                  onClick={() => setSelected([])}
                >
                  Clear selection
                </Button>
              </div>
              {result.truncated && (
                <Alert variant="warning">
                  <AlertTitle>Partial upstream list</AlertTitle>
                  <AlertDescription>
                    The gateway reached its model discovery limit. You can still import the models shown here.
                  </AlertDescription>
                </Alert>
              )}
              <div className="min-h-0 overflow-auto rounded-lg border">
                {filtered.length === 0 ? (
                  <StateMessage
                    role="status"
                    icon={<Search className="size-5" />}
                    title={result.models.length === 0 ? "No upstream models found" : "No matching models"}
                    description={
                      result.models.length === 0
                        ? "The upstream returned an empty model list for these credentials."
                        : "Try a different model name."
                    }
                  />
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="w-12">
                          <span className="sr-only">Selected</span>
                        </TableHead>
                        <TableHead>Upstream model ID</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {pageModels.map((model) => (
                        <TableRow key={model} data-state={selectedSet.has(model) ? "selected" : undefined}>
                          <TableCell>
                            <Checkbox
                              aria-label={`Select ${model}`}
                              checked={selectedSet.has(model)}
                              onCheckedChange={(checked) =>
                                setSelected((current) =>
                                  checked
                                    ? uniqueModelIds([...current, model])
                                    : current.filter((item) => item !== model),
                                )
                              }
                            />
                          </TableCell>
                          <TableCell className="break-all font-mono text-xs">{model}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </div>
              {pageCount > 1 && (
                <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
                  <span>
                    Page {currentPage} of {pageCount}. Select all results includes every page.
                  </span>
                  <div className="flex gap-1">
                    <Button
                      type="button"
                      variant="outline"
                      size="icon-xs"
                      aria-label="Previous upstream models page"
                      disabled={currentPage <= 1}
                      onClick={() => setPage(currentPage - 1)}
                    >
                      <ChevronLeft />
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="icon-xs"
                      aria-label="Next upstream models page"
                      disabled={currentPage >= pageCount}
                      onClick={() => setPage(currentPage + 1)}
                    >
                      <ChevronRight />
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
          <DialogFooter className="items-center sm:justify-between">
            <p className="text-xs text-muted-foreground">
              {selected.length} selected{result ? ` · ${filtered.length} of ${result.count} models shown` : ""}
            </p>
            <div className="flex gap-2">
              <Button type="button" variant="outline" onClick={() => changeOpen(false)}>
                Cancel
              </Button>
              {error && (
                <Button type="button" variant="outline" onClick={() => void fetchModels()}>
                  Retry
                </Button>
              )}
              <Button type="button" disabled={loading || selected.length === 0} onClick={importSelected}>
                {loading && <Loader2 className="animate-spin" />}Import selected
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
