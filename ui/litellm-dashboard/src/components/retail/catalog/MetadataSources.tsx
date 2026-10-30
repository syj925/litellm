import { ArrowUpRight } from "lucide-react";
import type { CatalogMetadataSource, CatalogModel } from "./types";

const sourceLabels: Readonly<Record<CatalogMetadataSource["source"], string>> = {
  models_dev: "models.dev",
  openrouter: "OpenRouter",
  deployment: "Site model configuration",
  litellm: "LiteLLM reference catalog",
};

const fieldLabels: Readonly<Record<string, string>> = {
  description: "Description",
  provider: "Model author",
  name: "Model name",
  context_length: "Input context",
  context_window: "Context window",
  max_output_tokens: "Maximum output",
  input_modalities: "Input modalities",
  output_modalities: "Output modalities",
  mode: "Mode",
  "capabilities.tools": "Tool calling",
  "capabilities.vision": "Image input",
  "capabilities.reasoning": "Reasoning",
  "capabilities.prompt_caching": "Prompt caching",
  "capabilities.structured_output": "Structured output",
  "capabilities.audio_input": "Audio input",
  "capabilities.audio_output": "Audio output",
  "capabilities.pdf_input": "PDF input",
};

export function metadataDate(value: string | null): string {
  if (!value) return "Not synced yet";
  const date = new Date(value);
  const options: Intl.DateTimeFormatOptions = { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" };
  return Number.isNaN(date.getTime())
    ? "Sync date unavailable"
    : new Intl.DateTimeFormat("en-US", options).format(date);
}

function sourceUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password ? value : null;
  } catch {
    return null;
  }
}

export function modelMetadataSources(model: CatalogModel): CatalogMetadataSource[] {
  if (model.metadata_sources?.length) return model.metadata_sources;
  if (!model.metadata_source) return [];
  return [
    {
      source: model.metadata_source === "deployment" ? "deployment" : "litellm",
      url: null,
      retrieved_at: null,
      fields: [],
      model_id: null,
    },
  ];
}

export function MetadataSourceSummary({ model }: { model: CatalogModel }) {
  const sources = modelMetadataSources(model);
  const names = [...new Set(sources.map(({ source }) => sourceLabels[source]))];
  const dates = sources.flatMap(({ retrieved_at }) => (retrieved_at ? [retrieved_at] : [])).sort();
  return (
    <p className="mt-3 text-[11px] leading-5 text-muted-foreground">
      {names.length ? `Sources: ${names.join(" · ")}` : "Model specifications are not published"}
      {dates.length > 0 && <> · Synced {metadataDate(dates[dates.length - 1])}</>}
      <span className="block">Reference specifications; channel support may vary.</span>
    </p>
  );
}

export function MetadataSourceDetails({ model }: { model: CatalogModel }) {
  const sources = modelMetadataSources(model);
  return (
    <section aria-labelledby="catalog-sources-title" className="rounded-lg border p-4">
      <h2 id="catalog-sources-title" className="text-sm font-semibold">
        Data sources
      </h2>
      <p className="mt-2 text-xs leading-6 text-muted-foreground">
        These are reference specifications. Capabilities have not been verified on every channel. Prices use this
        site&apos;s pricing configuration; external catalog prices and performance metrics are not imported.
      </p>
      {sources.length === 0 ? (
        <p className="mt-3 text-xs text-muted-foreground">No specification source has been published.</p>
      ) : (
        <ul className="mt-3 divide-y">
          {sources.map((source, index) => {
            const href = sourceUrl(source.url);
            return (
              <li key={`${source.source}-${source.model_id ?? index}`} className="py-3 text-xs first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 font-medium hover:underline"
                    >
                      {sourceLabels[source.source]} <ArrowUpRight className="size-3" />
                    </a>
                  ) : (
                    <span className="font-medium">{sourceLabels[source.source]}</span>
                  )}
                  {source.retrieved_at && (
                    <span className="text-muted-foreground">Synced {metadataDate(source.retrieved_at)} UTC</span>
                  )}
                </div>
                {source.model_id && <p className="mt-1 break-all font-mono text-muted-foreground">{source.model_id}</p>}
                {source.fields.length > 0 && (
                  <p className="mt-1 leading-5 text-muted-foreground">
                    {source.fields.map((field) => fieldLabels[field] ?? field).join(", ")}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {(model.metadata_conflicts?.length ?? 0) > 0 && (
        <p className="mt-3 rounded-md bg-muted px-3 py-2 text-xs leading-5 text-muted-foreground">
          Sources disagree on {model.metadata_conflicts?.map((field) => fieldLabels[field] ?? field).join(", ")}. The
          catalog uses the selected source for each field. Check the source before relying on this specification.
        </p>
      )}
    </section>
  );
}
