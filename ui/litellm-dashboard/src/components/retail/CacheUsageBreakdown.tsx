import { cacheDurationLabel, cacheTierEntries } from "./cachePricing";
import type { CacheUsageSummary } from "./cacheUsage";

const numberFormat = new Intl.NumberFormat("en-US");

function amount(value: number | null | undefined): string {
  return value == null ? "Not recorded" : numberFormat.format(value);
}

export default function CacheUsageBreakdown({ usage }: { usage: CacheUsageSummary }) {
  const parts = cacheTierEntries(usage.cache_write_ttl_tokens);
  const unknown = usage.cache_write_unclassified_tokens ?? (parts.length ? null : usage.cache_write_tokens);
  return (
    <section aria-label="Prompt cache usage" className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Prompt cache usage</h3>
        <a href="https://docs.litellm.ai/docs/completion/prompt_caching" target="_blank" rel="noopener noreferrer"
          className="text-xs text-muted-foreground hover:underline">Prompt caching docs</a>
      </div>
      <dl className="mt-3 grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <dt className="text-xs text-muted-foreground">Cache read tokens</dt>
          <dd className="mt-1 tabular-nums">{amount(usage.cache_read_tokens)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Cache write tokens · total</dt>
          <dd className="mt-1 tabular-nums">{amount(usage.cache_write_tokens)}</dd>
        </div>
        {parts.map(([ttl, tokens]) => (
          <div key={ttl}>
            <dt className="text-xs text-muted-foreground">Cache write · {cacheDurationLabel(ttl)}</dt>
            <dd className="mt-1 tabular-nums">{amount(tokens)}</dd>
          </div>
        ))}
        <div>
          <dt className="text-xs text-muted-foreground">Cache write · unspecified duration</dt>
          <dd className="mt-1 tabular-nums">{amount(unknown)}</dd>
        </div>
      </dl>
      <p className="mt-3 text-xs leading-5 text-muted-foreground">
        Duration categories are parts of the write total, not additional tokens. Historical writes without a reported
        duration remain unspecified.
      </p>
    </section>
  );
}
