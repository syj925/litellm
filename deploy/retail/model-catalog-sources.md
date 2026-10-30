# Model catalog metadata

The public and personal model catalogs combine models.dev and OpenRouter metadata with existing deployment declarations and LiteLLM fallback specifications. External records enrich only model descriptions, display names, modalities, limits and declared capabilities. Request model IDs, channel settings, prices, discounts, wallets and billing remain independent

The catalog joins exact canonical model IDs derived from the deployment's base model or upstream model field. It can also use models.dev's explicit mapping from an OpenRouter model ID to its canonical model. It does not remove dates, change version punctuation heuristically, select preview variants or match similar names. Multiple upstream identities under one public alias retain their original consensus metadata with an identity-conflict notice

OpenRouter descriptions take precedence when matched; models.dev fills missing descriptions. Other specifications prefer models.dev, then OpenRouter, then existing LiteLLM metadata. Explicit deployment capability and limit overrides retain priority. Sources that disagree are recorded per field. Missing declarations remain unknown. A source's capability claim does not prove that every configured channel supports it

`context_length` remains the maximum input-token declaration. The separate `context_window` represents total context. OpenRouter's total context never overwrites the input limit. Generic file input does not establish PDF support

## Synchronization and operations

In Models + Endpoints, a writable proxy administrator can open Catalog data to inspect sources and select Sync now. Ordinary accounts cannot read or refresh the administrative source status. Model list cards and details show public source attribution, retrieval dates and the fields each source supplies

`GET /retail/admin/catalog-sources` returns source status. `POST /retail/admin/catalog-sources/sync` refreshes both sources. Snapshots are stored separately in `retail_catalog_sources`; page reads use saved snapshots and make no external requests. Concurrent workers share a PostgreSQL advisory lock. Each source retains its last good data after a fetch or validation failure, independently of the other source

The included retail lifespan starts a nonblocking refresh loop. Refresh is due every six hours. Set `RETAIL_CATALOG_SYNC_ENABLED=false` to disable automatic refresh while retaining administrator-triggered refresh. Downloads use fixed public HTTPS endpoints, bounded time and response size, and do not send provider credentials

The normalized metadata excludes all external pricing and performance fields. Existing site reference prices and discounts continue to determine displayed rates and charges. TTFT, throughput and reliability measurements must come from site observations; no external benchmark values are presented as this site's performance

## Sources

models.dev provides `https://models.dev/models.json` for model facts and `https://models.dev/api.json` for declared provider settings and explicit canonical mappings. OpenRouter provides `https://openrouter.ai/api/v1/models`. These are maintained reference sources rather than independent channel tests. Their descriptions are rendered as text rather than executable HTML

models.dev is a community-maintained MIT-licensed project. Its notice is retained in `THIRD_PARTY_NOTICES.md`. OpenRouter metadata is attributed to OpenRouter and accessed through its documented public API
