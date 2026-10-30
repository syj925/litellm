export interface CacheUsageSummary {
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  cache_write_ttl_tokens?: Record<string, number>;
  cache_write_unclassified_tokens?: number | null;
}

function record(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function count(value: unknown): number | null {
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
  return null;
}

function ttlParts(value: unknown): Record<string, number> {
  return Object.fromEntries(
    Object.entries(record(value)).flatMap(([key, amount]) => {
      const tokens = count(amount);
      return /^\d+$/.test(key) && Number.isSafeInteger(Number(key)) && Number(key) > 0 && Number(key) <= 31536000 && tokens !== null
        ? [[key, tokens]]
        : [];
    }),
  );
}

export function logCacheUsage(log: { cache_usage?: CacheUsageSummary | null; metadata?: unknown }): CacheUsageSummary {
  if (log.cache_usage) return log.cache_usage;
  const usage = record(record(log.metadata).additional_usage_values);
  const details = record(usage.prompt_tokens_details ?? usage.input_tokens_details);
  const read = count(usage.cache_read_input_tokens) ?? count(details.cached_tokens);
  const write = count(usage.cache_creation_input_tokens) ?? count(details.cache_write_tokens) ?? count(details.cache_creation_tokens);
  const duration = record(details.cache_creation_token_details ?? usage.cache_creation);
  const openai = record(details.cache_write_tokens_details);
  const rawParts = {
    ...(count(duration.ephemeral_5m_input_tokens) != null ? { "300": duration.ephemeral_5m_input_tokens } : {}),
    ...(count(duration.ephemeral_1h_input_tokens) != null ? { "3600": duration.ephemeral_1h_input_tokens } : {}),
    ...(count(openai.ttl_30m_tokens) != null ? { "1800": openai.ttl_30m_tokens } : {}),
    ...(count(openai.ttl_12h_tokens) != null ? { "43200": openai.ttl_12h_tokens } : {}),
  };
  const parts = ttlParts(rawParts);
  const classified = Object.values(parts).reduce((sum, amount) => sum + amount, 0);
  const valid = write != null && classified <= write;
  return {
    cache_read_tokens: read,
    cache_write_tokens: write,
    cache_write_ttl_tokens: valid ? parts : {},
    cache_write_unclassified_tokens: write == null ? null : write - (valid ? classified : 0),
  };
}
