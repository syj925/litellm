# Cache usage compatibility

The local NewAPI checkout was inspected at commit `142436bc0` on 2026-10-30 without modification. Its host DTO aliases resolve to the checked-in `relaykit` module through `go.mod`. The following rules come from that source checkout, rather than an assumption that all compatible gateways use the official OpenAI field names

| Source evidence in NewAPI | Observed rule |
| --- | --- |
| `relay/channel/openai/usage.go:10` | Provider postprocessing supplements missing canonical cache counts for DeepSeek, Zhipu, Moonshot and llama.cpp |
| `relay/channel/openai/usage.go:53` | Raw usage extraction recognizes nested `prompt_tokens_details.cached_tokens`, top-level `cached_tokens` and `prompt_cache_hit_tokens` |
| `relay/channel/openai/usage.go:86` | Moonshot can report cached tokens under `choices[].usage.cached_tokens` |
| `relay/channel/openai/usage.go:114` | llama.cpp can report cache reads as `timings.cache_n` |
| `relaykit/dto/openai_response.go:316` | `cached_creation_tokens` and `cache_write_tokens` describe aliases of writes, so the larger value wins instead of summing them |
| `service/billing_usage.go:19` and `relaykit/dto/billing_usage.go:241` | Original provider usage snapshots take precedence over converted response shapes; an empty snapshot cannot erase real usage |
| `relaykit/dto/billing_usage.go:300` | Anthropic input excludes cache reads and writes |
| `relaykit/dto/billing_usage.go:331` | Gemini input includes prompt and tool-use input, cachedContentTokenCount is the cache-read subset, and candidate plus thought counts form output |
| `service/text_quota.go:341` | Explicit Claude duration counts use their duration prices, while the remaining write count still uses the generic creation price |
| `model/model_pricing_conversion.go:134` | Display mode is separate from the engine's internal fallback rate; a fallback alone does not create a published cache-write pricing category |

NewAPI's protocol conversion helper `relaykit/relayconvert/internal/shared/claude/cache.go:3` assigns unclassified creation remainder to 5m. Its newer original-usage accounting snapshot preserves reported duration evidence instead. This implementation retains the reported evidence and uses the default price when necessary, without inventing a reported 5m or 1h count

The retail normalizer recognizes canonical cache counts and established aliases across prompt/input detail containers. Read aliases include `cached_tokens`, `prompt_cache_hit_tokens` and Anthropic's `cache_read_input_tokens`; write aliases include `cache_creation_input_tokens`, `cache_write_tokens`, `cache_creation_tokens` and `cached_creation_tokens`. Additional accepted read spellings, `cache_read_tokens` and `cache_hit_tokens`, are local compatibility rules, not claims about the inspected NewAPI code. Native Gemini usage and the known Moonshot/llama.cpp response locations are normalized before accounting

Aliases are never summed as separate categories. A cache miss count is not a cache write count. Duration subsets remain part of the write total. Negative, fractional or contradictory quantities are not converted into a successful free bill. Model branding and the client-facing endpoint alone do not determine whether the original input total includes cache categories

New requests freeze token-v3 pricing. An explicitly configured duration rate wins; otherwise the frozen generic write rate applies, including an explicit zero. The final pool/model discount applies to both paths. The actual duration remains in the usage and charge breakdown, and a receipt using the fallback identifies the saved default unit rate. A request without duration information uses the generic rate without inventing a duration

Older token-v2 requests continue rejecting missing duration prices during settlement, and flat-token-v1 requests retain their original flat cache-write calculation. Existing financial snapshots and settled bills are not migrated or repriced. Missing total usage or invalid evidence remains pending rather than being guessed

The public price catalog describes configured or published categories. It does not promote an input-price fallback into an independently published cache-write charge. Duration-priced models display their duration rows, while models with a generic write rate display a normal Cache write row. Detailed logs can still identify writes whose duration was not reported

The nullable `cache_write_price_published` column retains that distinction after automatic price initialization or a catalog import. Catalog cards derive it from the presence of the original creation-price field. Manual price saves set it to true, including explicit zero prices. The marker affects published generic price views, not internal default rates or frozen charges. Old null catalog rows use their exact catalog model to resolve publication for the view; missing source evidence does not become a published generic rate. Manual and legacy configured rows remain visible. Migration adds metadata without rewriting old price values, versions or financial snapshots

The common native completion conversion also normalizes recognized choice-level and timing cache reads before native cost calculation. It preserves positive canonical counts and original response fields. Synchronous and asynchronous cache replay retain the full usage object. This keeps native cost breakdowns, spend-log cache counts and retail accounting on the same recognized quantities

Native spend timestamps are converted to UTC and truncated to milliseconds before serialization. The observation uses the same precision, so PostgreSQL timestamp rounding cannot move a native log one millisecond away from its observation. Correlation still requires the same call ID, account and exact start time; no time-tolerance join or historical rewrite is introduced

The nullable `retail_model_prices.cache_write_price_published` field records publication evidence separately from the internal generic amount. New catalog snapshots record whether the original catalog actually supplied a write price. Automatic initialization and catalog import preserve this flag; manual saves explicitly publish their configured generic price, including zero. Management responses expose the flag, but manual requests cannot set it. A false flag hides only the generic public price and does not suppress duration rates or alter settlement

The migration adds this metadata column without rewriting old rows, price versions, reservation snapshots or bills. Existing null catalog flags are resolved read-only from the exact catalog_model's current raw field presence, with no generic publication when that source is unavailable. Existing manual and legacy configured rates remain visible. Persisted true or false flags do not change when later catalog metadata changes
