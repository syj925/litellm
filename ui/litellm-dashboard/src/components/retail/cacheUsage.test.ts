import { describe, expect, it } from "vitest";
import { logCacheUsage } from "./cacheUsage";

describe("recorded cache duration usage", () => {
  it("keeps historical writes unspecified and never infers a model's default duration", () => {
    expect(logCacheUsage({ metadata: { additional_usage_values: { cache_creation_input_tokens: 10 } } })).toEqual({
      cache_read_tokens: null, cache_write_tokens: 10, cache_write_ttl_tokens: {}, cache_write_unclassified_tokens: 10,
    });
  });

  it("keeps OpenAI TTL counts inside their total instead of adding them again", () => {
    expect(logCacheUsage({ metadata: { additional_usage_values: { prompt_tokens_details: {
      cached_tokens: 2, cache_write_tokens: 10,
      cache_write_tokens_details: { ttl_30m_tokens: 3, ttl_12h_tokens: 5 },
    } } } })).toEqual({
      cache_read_tokens: 2, cache_write_tokens: 10, cache_write_ttl_tokens: { "1800": 3, "43200": 5 },
      cache_write_unclassified_tokens: 2,
    });
  });

  it("does not present invalid over-reported durations as trusted classifications", () => {
    const usage = logCacheUsage({ metadata: { additional_usage_values: { cache_creation_input_tokens: 10,
      prompt_tokens_details: { cache_creation_token_details: { ephemeral_5m_input_tokens: 10, ephemeral_1h_input_tokens: 10 } },
    } } });
    expect(usage.cache_write_ttl_tokens).toEqual({});
    expect(usage.cache_write_unclassified_tokens).toBe(10);
  });
});
