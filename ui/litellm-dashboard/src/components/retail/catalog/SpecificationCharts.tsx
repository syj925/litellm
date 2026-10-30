import { catalogModelTitle, formatContext } from "./modelIdentity";
import type { CatalogModel } from "./types";

const specifications = [
  { field: "context_window", label: "Total context window" },
  { field: "context_length", label: "Maximum input" },
  { field: "max_output_tokens", label: "Maximum output" },
] as const;

type TokenLimit = (typeof specifications)[number]["field"];

export function tokenLimit(model: CatalogModel, field: TokenLimit): number | null {
  const value = model[field];
  return value != null && Number.isFinite(value) && value > 0 ? value : null;
}

export default function SpecificationCharts({ models }: { models: CatalogModel[] }) {
  if (models.length < 2) return null;
  return (
    <section aria-labelledby="specification-charts-title" className="border-b bg-muted/15 p-4">
      <h3 id="specification-charts-title" className="text-sm font-semibold">Specification comparison</h3>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">
        Token limits from the declared model specifications. Each chart has its own scale; these are not quality scores.
      </p>
      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        {specifications.map(({ field, label }) => {
          const maximum = Math.max(0, ...models.map((model) => tokenLimit(model, field) ?? 0));
          return (
            <figure key={field} aria-label={`${label} comparison`} className="min-w-0 rounded-md border bg-background p-3">
              <figcaption className="mb-4 text-xs font-medium">{label}</figcaption>
              <div className="space-y-4">
                {models.map((model, index) => {
                  const value = tokenLimit(model, field);
                  return (
                    <div key={model.id} className="min-w-0">
                      <div className="mb-1.5 flex min-w-0 items-start justify-between gap-3 text-[11px]">
                        <span className="min-w-0 truncate text-muted-foreground" title={model.id}>{catalogModelTitle(model)}</span>
                        <span className="shrink-0 font-medium tabular-nums">{formatContext(value)}</span>
                      </div>
                      {value == null ? (
                        <div className="h-2.5 rounded-sm border border-dashed" aria-label={`${catalogModelTitle(model)} limit not specified`} />
                      ) : (
                        <div
                          role="meter"
                          aria-label={`${catalogModelTitle(model)} ${label}`}
                          aria-valuenow={value}
                          aria-valuemin={0}
                          aria-valuemax={maximum}
                          aria-valuetext={`${value.toLocaleString("en-US")} tokens`}
                          className="h-2.5 overflow-hidden rounded-sm bg-muted"
                        >
                          <div
                            className={`h-full rounded-sm ${["bg-blue-500", "bg-violet-500", "bg-teal-500"][index % 3]}`}
                            style={{ width: `${100 * value / maximum}%` }}
                          />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </figure>
          );
        })}
      </div>
    </section>
  );
}
