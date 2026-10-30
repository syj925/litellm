"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowLeft, ArrowUpRight, Check, Code2, Layers, LockKeyhole } from "lucide-react";
import { Logo } from "@/components/molecules/logo/Logo";
import CopyButton from "@/components/shared/CopyButton";
import CodeBlock from "@/components/CodeBlock";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { uiHref } from "@/utils/uiHref";
import { sourceKindLabel } from "../pools/types";
import { getModelIdentity, formatContext } from "./modelIdentity";
import { catalogDescription } from "./catalogState";
import { MetadataSourceDetails } from "./MetadataSources";
import ModelPerformance from "./ModelPerformance";
import ModelBenchmarks from "./ModelBenchmarks";
import {
  catalogDetailTabs,
  catalogPriceRows,
  discountLabel,
  formatCatalogPrice,
  modelPrice,
  type CatalogCapabilities,
  type CatalogDetailTab,
  type CatalogModel,
  type CatalogOffer,
} from "./types";
import {
  catalogApiExample,
  catalogApiProtocols,
  type CatalogApiLanguage,
  type CatalogApiProtocol,
} from "./catalogApiExamples";

interface CatalogModelDetailProps {
  model: CatalogModel;
  activeTab: CatalogDetailTab;
  onTabChange: (tab: CatalogDetailTab) => void;
  onBack: () => void;
  apiBaseUrl: string;
  publicView?: boolean;
  accessToken?: string | null;
}

interface ModelSectionProps {
  model: CatalogModel;
  publicView: boolean;
}

const capabilityLabels: Readonly<Record<keyof CatalogCapabilities, string>> = {
  vision: "Image input",
  tools: "Tool calling",
  reasoning: "Reasoning",
  prompt_caching: "Prompt caching",
  structured_output: "Structured output",
  audio_input: "Audio input",
  audio_output: "Audio output",
  pdf_input: "PDF input",
};

const sourceLogoProviders: Readonly<Record<string, string>> = {
  bedrock: "bedrock",
  vertex: "vertex_ai",
  anthropic: "anthropic",
  anthropic_subscribe: "anthropic",
  openai: "openai",
};

const tabLabels: Readonly<Record<CatalogDetailTab, string>> = {
  overview: "Overview",
  resources: "Resources",
  performance: "Performance",
  benchmarks: "Benchmarks",
  pricing: "Pricing",
  api: "API",
};

const languageLabels: Readonly<Record<CatalogApiLanguage, string>> = {
  curl: "cURL",
  python: "Python",
  javascript: "JavaScript",
};

function capabilitySupport(value: boolean | null): string {
  if (value === true) return "Supported";
  return value === false ? "Not supported" : "Not specified";
}

function integrationAccessNote(model: CatalogModel, publicView: boolean): string {
  if (!model.offers.length) {
    return "No resource pool is published for this model yet. You can still explore its integration reference.";
  }
  if (publicView) return "Sign in to check your resource pool access and create an API key.";
  if (model.available_to_me) {
    return "Choose the resource pools your API key can use. Its permissions determine available models.";
  }
  return "Resource pool access is required before calling this model. Contact your administrator to enable it.";
}

function Availability({ available }: { available: boolean }) {
  return (
    <Badge variant="outline" className={available ? "gap-1 text-success" : "gap-1 text-muted-foreground"}>
      {available ? <Check /> : <LockKeyhole />}
      {available ? "Available to you" : "Access required"}
    </Badge>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 p-4">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 font-medium tabular-nums">{value}</dd>
    </div>
  );
}

function CatalogOverview({
  model,
  publicView,
  onTabChange,
}: ModelSectionProps & { onTabChange: (tab: CatalogDetailTab) => void }) {
  const referenceInput = modelPrice(model, "reference", "input");
  const referenceOutput = modelPrice(model, "reference", "output");
  const protocols = catalogApiProtocols(model);
  return (
    <div className="grid gap-8 pt-5 lg:grid-cols-[minmax(0,1fr)_280px]">
      <div className="min-w-0 space-y-7">
        <section aria-labelledby="catalog-about-title">
          <h2 id="catalog-about-title" className="font-semibold">
            About this model
          </h2>
          <p className="mt-3 max-w-3xl whitespace-pre-line text-sm leading-7 text-muted-foreground">
            {catalogDescription(model)}
          </p>
        </section>
        <section aria-labelledby="catalog-capabilities-title">
          <h2 id="catalog-capabilities-title" className="font-semibold">
            Capabilities
          </h2>
          <dl className="mt-3 grid divide-y rounded-lg border sm:grid-cols-2 sm:divide-y-0">
            {Object.entries(capabilityLabels).map(([key, label]) => {
              const value = model.capabilities[key as keyof CatalogCapabilities];
              return (
                <div
                  key={key}
                  className="flex items-center justify-between gap-3 border-border px-4 py-3 text-sm sm:odd:border-r"
                >
                  <dt>{label}</dt>
                  <dd className={value === true ? "text-success" : "text-muted-foreground"}>
                    {capabilitySupport(value)}
                  </dd>
                </div>
              );
            })}
          </dl>
        </section>
        <p className="text-xs leading-6 text-muted-foreground">
          Not specified means this information has not been published, rather than that the feature is unsupported.
        </p>
        <section className="rounded-lg border p-5" aria-labelledby="catalog-integration-title">
          <div className="flex items-start gap-3">
            <div className="rounded-md bg-muted p-2 text-muted-foreground">
              <Code2 className="size-4" />
            </div>
            <div>
              <h2 id="catalog-integration-title" className="font-semibold">
                Integrate this model
              </h2>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                Use the exact model ID in your requests. Explore examples for your preferred SDK.
              </p>
            </div>
          </div>
          <div className="mt-4 flex items-center justify-between gap-3 rounded-md bg-muted/40 px-4 py-3">
            <div className="min-w-0">
              <p className="text-xs text-muted-foreground">Request model ID</p>
              <code className="mt-1 block break-all text-sm">{model.id}</code>
            </div>
            <CopyButton value={model.id} label="Copy request model ID" />
          </div>
          {protocols.length > 0 && (
            <div className="mt-4 flex flex-wrap items-center gap-2 text-xs">
              <span className="mr-1 text-muted-foreground">API example formats</span>
              {protocols.map((protocol) => (
                <Badge key={protocol} variant="outline">
                  {protocol === "anthropic" ? "Anthropic Messages" : "OpenAI compatible"}
                </Badge>
              ))}
            </div>
          )}
          <p className="mt-4 text-xs leading-6 text-muted-foreground">{integrationAccessNote(model, publicView)}</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button size="sm" onClick={() => onTabChange("api")}>
              {protocols.length ? "View API examples" : "View integration reference"} <ArrowUpRight />
            </Button>
            {model.offers.length > 0 && (
              <Button size="sm" variant="outline" onClick={() => onTabChange("resources")}>
                <Layers /> View resource pools
              </Button>
            )}
          </div>
        </section>
        <details className="text-xs text-muted-foreground">
          <summary className="w-fit cursor-pointer rounded-sm hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring">
            View data sources
          </summary>
          <div className="mt-3">
            <MetadataSourceDetails model={model} />
          </div>
        </details>
      </div>
      <aside className="space-y-5">
        <section className="rounded-lg border" aria-labelledby="catalog-specifications-title">
          <h2 id="catalog-specifications-title" className="border-b px-4 py-3 text-sm font-semibold">
            Specifications
          </h2>
          <dl className="divide-y">
            {model.context_window != null && (
              <Metric label="Total context window" value={formatContext(model.context_window)} />
            )}
            <Metric label="Input context length" value={formatContext(model.context_length)} />
            <Metric label="Maximum output" value={formatContext(model.max_output_tokens)} />
            <Metric label="Input modalities" value={model.input_modalities.join(", ") || "Not specified"} />
            <Metric label="Output modalities" value={model.output_modalities.join(", ") || "Not specified"} />
            <Metric label="Mode" value={model.mode || "Not specified"} />
          </dl>
        </section>
        <section className="rounded-lg border p-4" aria-labelledby="catalog-reference-title">
          <h2 id="catalog-reference-title" className="text-sm font-semibold">
            Reference pricing
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">From, per 1M tokens</p>
          <dl className="mt-4 space-y-3 text-sm">
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Input</dt>
              <dd>{formatCatalogPrice(referenceInput?.toString())}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Output</dt>
              <dd>{formatCatalogPrice(referenceOutput?.toString())}</dd>
            </div>
          </dl>
          <p className="mt-4 text-xs leading-5 text-muted-foreground">
            {publicView
              ? "Reference rates may differ by resource pool. See Pricing for the published rates."
              : "Your price may differ by resource pool. See Pricing for your account's offers."}
          </p>
        </section>
      </aside>
    </div>
  );
}

function PoolIdentity({ offer }: { offer: CatalogOffer }) {
  return (
    <div className="flex items-center gap-3">
      <Logo
        provider={sourceLogoProviders[offer.source_kind] ?? offer.source_kind}
        className="size-7 shrink-0 object-contain"
      />
      <div className="min-w-0">
        <p className="font-medium">{offer.pool_name}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">{sourceKindLabel(offer.source_kind)}</p>
      </div>
    </div>
  );
}

function CatalogResources({ model, publicView }: ModelSectionProps) {
  return (
    <section className="space-y-5 pt-5" aria-label="Model resources">
      <div>
        <h2 className="font-semibold">Resource pools</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
          This model uses the same model ID across resource pools. Choose a fixed pool or allowed pool/model pairs when
          creating an API key to control its routing.
        </p>
      </div>
      <div className="divide-y overflow-hidden rounded-lg border">
        {model.offers.map((offer) => (
          <div key={offer.pool_id} className="flex flex-wrap items-center justify-between gap-5 p-5">
            <PoolIdentity offer={offer} />
            {!publicView && (
              <div className="flex flex-wrap items-center gap-5">
                <Availability available={offer.available_to_me} />
                {offer.available_to_me ? (
                  <Link href={uiHref("api-keys")} className={buttonVariants({ variant: "outline", size: "sm" })}>
                    Manage keys <ArrowUpRight />
                  </Link>
                ) : (
                  <span className="text-xs text-muted-foreground">Contact your administrator for access</span>
                )}
              </div>
            )}
          </div>
        ))}
        {!model.offers.length && (
          <p className="p-8 text-center text-sm text-muted-foreground">
            No resource pools are published for this model.
          </p>
        )}
      </div>
      <p className="text-xs leading-6 text-muted-foreground">
        {publicView
          ? "Sign in to create an API key and choose a resource pool."
          : "Access is granted per resource pool and model. Creating a key cannot expand your account's permissions."}
      </p>
    </section>
  );
}

function PricingTable({ offer, publicView }: { offer: CatalogOffer; publicView: boolean }) {
  const rows = catalogPriceRows(offer.pricing.reference);
  const customerRows = new Map(catalogPriceRows(offer.pricing.customer).map(({ key, amount }) => [key, amount]));
  return (
    <div className="overflow-hidden rounded-lg border">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-muted/25 p-4">
        <PoolIdentity offer={offer} />
        {!publicView && <Availability available={offer.available_to_me} />}
      </div>
      <Table aria-label={"Pricing for " + offer.pool_name}>
        <TableHeader>
          <TableRow>
            <TableHead>Usage type</TableHead>
            <TableHead className="text-right">Reference / 1M tokens</TableHead>
            {!publicView && <TableHead className="text-right">Your price / 1M tokens</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map(({ key, label, amount }) => (
            <TableRow key={key}>
              <TableCell>{label}</TableCell>
              <TableCell className="text-right tabular-nums">{formatCatalogPrice(amount)}</TableCell>
              {!publicView && (
                <TableCell className="text-right tabular-nums">
                  {offer.available_to_me ? formatCatalogPrice(customerRows.get(key)) : "Access required"}
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {!publicView && offer.available_to_me && (
        <div className="border-t px-4 py-3 text-xs leading-6 text-muted-foreground">
          {discountLabel(offer.pricing.discount) || "Your price has not been published."}
          {offer.pricing.source_label && <span> · {offer.pricing.source_label}</span>}
        </div>
      )}
    </div>
  );
}

function CatalogPricingPanel({ model, publicView }: ModelSectionProps) {
  return (
    <section className="space-y-5 pt-5" aria-label="Model pricing">
      <div>
        <h2 className="font-semibold">Pricing by resource pool</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
          {publicView
            ? "Published reference rates for each resource pool."
            : "Reference prices are the site's published price basis. Your price includes the discount configured for your account and the selected resource pool."}
        </p>
      </div>
      {model.offers.map((offer) => (
        <PricingTable key={offer.pool_id} offer={offer} publicView={publicView} />
      ))}
      {!model.offers.length && model.reference_pricing && (
        <div className="overflow-hidden rounded-lg border">
          <h3 className="border-b bg-muted/25 px-4 py-3 text-sm font-semibold">Reference pricing</h3>
          <Table aria-label="Reference pricing">
            <TableHeader>
              <TableRow>
                <TableHead>Usage type</TableHead>
                <TableHead className="text-right">USD / 1M tokens</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {catalogPriceRows(model.reference_pricing).map(({ key, label, amount }) => (
                <TableRow key={key}>
                  <TableCell>{label}</TableCell>
                  <TableCell className="text-right">{formatCatalogPrice(amount)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {!model.offers.length && !model.reference_pricing && (
        <p className="rounded-lg border p-8 text-center text-sm text-muted-foreground">
          Pricing has not been published for this model.
        </p>
      )}
      <p className="text-xs leading-6 text-muted-foreground">
        Prices are in USD per 1M tokens. Each cache write duration is charged at its complete write price, without
        adding ordinary input again. Unspecified duration means the upstream did not identify a write duration.
        Additional context tiers may apply. Not listed means no price is published for that usage type.
        {!publicView && (
          <>
            {" "}
            Your settled charges are recorded in{" "}
            <Link href={uiHref("billing")} className="underline">
              Billing
            </Link>
            .
          </>
        )}
      </p>
    </section>
  );
}

function CatalogApiPanel({ model, apiBaseUrl, publicView }: ModelSectionProps & { apiBaseUrl: string }) {
  const protocols = catalogApiProtocols(model);
  const [selectedProtocol, setSelectedProtocol] = useState<CatalogApiProtocol>("openai");
  const [language, setLanguage] = useState<CatalogApiLanguage>("curl");
  const protocol = protocols.includes(selectedProtocol) ? selectedProtocol : "openai";
  const example = catalogApiExample(model, apiBaseUrl, protocol, language);
  return (
    <section className="space-y-5 pt-5" aria-label="Model API examples">
      <div>
        <h2 className="font-semibold">Use this model</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
          Copy the exact model ID below. Set LITELLM_API_KEY to your own AI API key. Use a key scoped to the resource
          pool you want to call.
        </p>
      </div>
      <div className="flex items-center justify-between gap-3 rounded-lg border bg-muted/25 px-4 py-3">
        <code className="min-w-0 break-all text-sm">{model.id}</code>
        <CopyButton value={model.id} label="Copy model ID" />
      </div>
      {!publicView && !model.available_to_me && (
        <p className="rounded-lg border bg-muted/25 p-4 text-sm leading-6 text-muted-foreground">
          You can read these examples, but model invocation requires access. Contact your administrator to enable a
          resource pool, then create an API key.
        </p>
      )}
      {protocols.length ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div role="group" aria-label="Example language" className="flex gap-1 rounded-lg border p-1">
              {(["curl", "python", "javascript"] as const).map((value) => (
                <Button
                  key={value}
                  size="sm"
                  variant={language === value ? "secondary" : "ghost"}
                  aria-pressed={language === value}
                  onClick={() => setLanguage(value)}
                >
                  {languageLabels[value]}
                </Button>
              ))}
            </div>
            <Select
              value={protocol}
              onValueChange={(value) => {
                if (value === "openai" || value === "anthropic") setSelectedProtocol(value);
              }}
            >
              <SelectTrigger aria-label="API format" className="min-w-48">
                <SelectValue>{protocol === "anthropic" ? "Anthropic Messages" : "OpenAI compatible"}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {protocols.map((value) => (
                  <SelectItem key={value} value={value}>
                    {value === "anthropic" ? "Anthropic Messages" : "OpenAI compatible"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div role="region" aria-label="Request code">
            <CodeBlock code={example} language={language === "curl" ? "bash" : language} />
          </div>
        </>
      ) : (
        <div className="rounded-lg border p-5 text-sm leading-6 text-muted-foreground">
          A request format has not been published for this model&apos;s mode. Check{" "}
          <Link href={uiHref("api-reference")} className="underline">
            API Reference
          </Link>{" "}
          or contact your administrator before integrating it.
        </div>
      )}
      <Link href={uiHref(publicView ? "login" : "api-keys")} className={buttonVariants({ variant: "outline" })}>
        <Code2 /> {publicView ? "Sign in to use" : "Manage API keys"}
      </Link>
    </section>
  );
}

export default function CatalogModelDetail({
  model,
  activeTab,
  onTabChange,
  onBack,
  apiBaseUrl,
  publicView = false,
  accessToken,
}: CatalogModelDetailProps) {
  const identity = getModelIdentity(model.id, model.provider);
  const displayName = model.name && model.name !== model.id ? model.name : identity.displayName;
  const availableResources = model.offers.filter((offer) => offer.available_to_me).length;
  return (
    <div className="space-y-6">
      <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2">
        <ArrowLeft /> Back to models
      </Button>
      <header className="flex flex-wrap items-start justify-between gap-5">
        <div className="flex min-w-0 items-start gap-4">
          <div className="flex size-14 shrink-0 items-center justify-center rounded-xl border bg-background">
            <Logo provider={identity.providerSlug} label={identity.author} className="size-8 object-contain" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">{displayName}</h1>
              {!publicView && <Availability available={model.available_to_me} />}
            </div>
            <p className="mt-1 text-sm text-muted-foreground">by {identity.author}</p>
            <div className="mt-2 flex min-w-0 items-center gap-2">
              <code className="break-all text-xs text-muted-foreground">{model.id}</code>
              <CopyButton value={model.id} label="Copy model ID" />
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {!publicView && model.available_to_me && (
            <Link href={uiHref("playground")} className={buttonVariants({ variant: "outline" })}>
              Open playground <ArrowUpRight />
            </Link>
          )}
          <Link href={uiHref(publicView ? "login" : "api-keys")} className={buttonVariants()}>
            {publicView ? "Sign in to use" : "Manage API keys"}
          </Link>
        </div>
      </header>
      <dl className="grid grid-cols-2 divide-x rounded-lg border md:grid-cols-4">
        <Metric label="Input context length" value={formatContext(model.context_length)} />
        <Metric label="Maximum output" value={formatContext(model.max_output_tokens)} />
        <Metric label="Resource pools" value={model.offers.length.toString()} />
        {publicView ? (
          <Metric label="Mode" value={model.mode || "Not specified"} />
        ) : (
          <Metric
            label="Available to you"
            value={
              availableResources
                ? availableResources.toString() + (availableResources === 1 ? " pool" : " pools")
                : "Access required"
            }
          />
        )}
      </dl>
      <Tabs
        value={activeTab}
        onValueChange={(value) => {
          const tab = catalogDetailTabs.find((candidate) => candidate === value);
          if (tab) onTabChange(tab);
        }}
        className="gap-0"
      >
        <TabsList variant="line" className="h-auto w-full justify-start overflow-x-auto border-b pb-1">
          {catalogDetailTabs.map((tab) => (
            <TabsTrigger key={tab} value={tab} className="flex-none px-4 py-2">
              {tab === "resources" && <Layers />}
              {tabLabels[tab]}
              {tab === "resources" && <span className="ml-1 text-xs text-muted-foreground">{model.offers.length}</span>}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview">
          <CatalogOverview model={model} publicView={publicView} onTabChange={onTabChange} />
        </TabsContent>
        <TabsContent value="resources">
          <CatalogResources model={model} publicView={publicView} />
        </TabsContent>
        <TabsContent value="performance">
          <ModelPerformance model={model.id} publicView={publicView} accessToken={accessToken} />
        </TabsContent>
        <TabsContent value="benchmarks">
          <ModelBenchmarks models={[model]} />
        </TabsContent>
        <TabsContent value="pricing">
          <CatalogPricingPanel model={model} publicView={publicView} />
        </TabsContent>
        <TabsContent value="api">
          <CatalogApiPanel model={model} apiBaseUrl={apiBaseUrl} publicView={publicView} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
