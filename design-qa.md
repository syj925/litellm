# Dynamic expression pricing QA

final result: passed for the reviewed local desktop states on 2026-10-30

Model Pricing supports fixed rates and expression rules, including context thresholds, separately priced cache durations, modalities, service tiers, weekday/weekend conditions, times and explicit calendar dates in an IANA timezone. Custom cache durations remain addressable in seconds. The editor includes synthetic templates and a server-side simulator using the actual billing evaluator. No JavaScript or Python evaluation is used

Before: C:/Users/26933/AppData/Local/Temp/codex-clipboard-78ee6509-fa8a-43fd-abdb-40369b4729df.png

Editor: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/expression-editor-proof.png

Peak simulation: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/expression-peak-proof.png

Weekend simulation: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/expression-weekend-proof.png

Public pricing: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/expression-public-pricing-proof.png

Actual browser review covered a loaded dynamic rule, an editable peak/weekend template, Asia/Shanghai time selection, matched branches and amounts, the anonymous model pricing page, rule counts and precise review reasons. Identical synthetic quantities produced USD 0.0072 during the example weekday peak and USD 0.0048 on its weekend branch. Every draft was cancelled. Browser warning and error collections were empty. The user's existing tabs were left untouched. Mobile and dark-theme acceptance were not performed

The final combined backend run passed 490 scoped tests. A real isolated PostgreSQL test verified additive migration, policy save/read and frozen historical snapshots. Eight scoped frontend files passed 106 tests. Production TypeScript, regenerated OpenAPI declarations, the webpack build and changed-file Ruff passed. UI lint retained existing warnings and pre-existing retail fetch-wrapper lint debt rather than adding suppressions

Full isolated HTTP validation passed 56 checks across eleven requests. Chat, Responses and Anthropic Messages used streaming and nonstreaming requests. Ten requests settled with ten unique charges; one missing-facts request retained its reservation. Exact costs, cache/context partitioning, actual returned image count, frozen time, policy and discount, duplicate callbacks and receipt hashes were verified. The fixture provider was scripted, so this does not establish live vendor usage reporting. No paid upstream call occurred. The fixture proxy and database were removed

The live administrator read API listed forty-four configured public model names. Seven had completely translated expression defaults; thirty-seven required manual pricing for unresolved dimensions such as regional factors, shared search allowances or unsupported usage categories. These remain visible with their original reference fields and named review reasons, without a partially translated active policy. Manual rules can be configured for any row. OpenAI and Anthropic search counters use loaded fees only when their dimensions can be preserved. No vendor schedule or rate is invented

Customer billing uses a frozen expression, version, request start, timezone and discount, then evaluates actual usage. Receipts and CSV retain the expression hash, matched tiers, quantities and final amount. Opaque input reservations use the frozen model capacity, output limits are checked before upstream calls, missing required facts remain pending and callbacks remain idempotent. Existing fixed rates and historical bills keep their original semantics. Background Batch submission and realtime sessions retain their endpoint restrictions

Local service health returned HTTP 200 after restart. Anonymous pricing simulation was denied with HTTP 401. A custom 750-second cache write simulation returned exactly USD 0.000513500 after its configured discount. Before/after row hashes matched for channels, native deployments, pool bindings, saved model prices and wallets. The isolated HTTP run independently reconciled wallet, price, ledger and reservation hashes. No actual model price, discount or balance was saved during QA

---

# Searchable model price configuration QA

final result: passed for the reviewed local desktop states on 2026-10-30

Model Pricing now automatically reads the site's configured public model names and their loaded default prices, together with saved overrides. The page supports model and reference-name search, provider, model type and price-source filters, URL-backed pagination and direct row editing. Provider icons identify each row. Edit actions stay visible while a wide table scrolls horizontally

Before: C:/Users/26933/AppData/Local/Temp/codex-clipboard-6d0e8940-778a-43b6-a3eb-916a5a5ed72b.png

After: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-pricing-catalog-proof.png

Search: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-pricing-catalog-search-proof.png

The authenticated local page displayed forty-four public model names, independently matching fifty-six deployment rows grouped by model name. Twenty-four safe built-in defaults and eleven base reference previews were available. Forty-two models had original numeric configuration fields to review, including context thresholds and service tiers. Non-token units stay in their original fields. Unknown entries remain visible without invented rates. Reference-only previews do not change which rate is effective

Actual browser checks covered provider filtering, seven image models under type filtering, model-name search, pagination, Claude 5m/1h form autofill and OpenAI 30m/12h configuration review followed by form autofill. Reviewing or editing required no copied model name. No form was saved during browser QA. The user's existing tab was left untouched. Captured browser warnings and errors were empty. Desktop states were reviewed at the available browser sizes; mobile and dark theme were not reviewed

The new administrator-only read endpoint excludes channel URLs, credentials and procurement rates. Default eligibility uses the same complete deployment-consensus check as request pricing. Saved overrides take precedence, including saved-only model names. A direct edit preserves an unpublished internal cache-write fallback when another field changes, while an explicitly edited zero write price remains publishable

The final backend run passed 172 scoped tests, including a real isolated PostgreSQL and ASGI save/read test. Twenty-nine scoped frontend tests, production TypeScript, regenerated OpenAPI declarations and the webpack build passed. Changed-file lint had no errors; four existing ModelPrices warning categories remain, and new table and review components were clean. Live read-only API checks reconciled effective prices and duration maps with reference and saved-price endpoints. Business fingerprints for channels, deployments, pool bindings, saved prices and wallets matched before and after. The local service returned healthy after restart

---

# Dedicated model pricing page QA

final result: passed for the reviewed local desktop state on 2026-10-30

Site-wide model base pricing now has its own administrator page at /ui/model-pricing/, with a Model Pricing entry under Billing and a matching command-palette entry. Customer Wallets retains balances, customer discounts and account access, with a link to the new page. The page keeps catalog import, manual base rates, cache read prices and cache write duration presets or custom seconds. Empty configured prices explain that catalog defaults remain available

Before: C:/Users/26933/AppData/Local/Temp/codex-clipboard-9efa27fe-bd45-4751-808a-ad7842418dde.png

After: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-pricing-page-proof.png

The actual authenticated local page was reviewed in the existing dashboard shell. The base-price section, actions and empty state rendered correctly, and captured browser warning and error collections were empty. The user's separate tab and open import dialog were left untouched. Mobile and dark-theme visual acceptance were not performed

Seventeen scoped pricing integration tests and 167 navigation, access and route tests passed. Ordinary users, organization administrators and proxy viewers cannot mount this administrative page. Legacy retail links preserve their query parameters, and the personal model-catalog route remains separate from the public models route. Production TypeScript and the webpack build passed, followed by a local service restart and HTTP 200 checks for health and the new page. Scoped lint added no errors; existing wallet-panel lint debt was not suppressed or expanded

No pricing APIs or billing calculations changed. Existing bills and reserved request snapshots retain their saved rates. Before and after business fingerprints matched for four channels, fifty-six native models, fifty-six pool bindings, zero saved model prices and four wallets. This QA did not import prices or change rates, balances or discounts

---

# Cache compatibility and price display correction QA

final result: passed for the reviewed local desktop states on 2026-10-30

Duration-priced models display only their duration rows. A configured generic write price retains the normal Cache write label. The public Claude detail shows 5m and 1h with no duplicate default category. Gemini's unconfigured creation rate is no longer manufactured from an internal input-price fallback; its generic row shows Not listed. Explicit manual generic rates, including zero, remain published. The nullable publication marker survives automatic price initialization and catalog import without repricing old records

Before: C:/Users/26933/AppData/Local/Temp/codex-clipboard-8010e335-f9a4-4525-b94e-8b945d5b18c5.png

Claude after: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/cache-compat-claude-pricing-proof.png

Gemini after: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/cache-compat-gemini-pricing-proof.png

Both actual anonymous pages were reviewed in the existing LiteLLM shell and had no captured browser warnings or errors. The administrator editor, generic-price comparison and billing receipt have targeted integration/unit coverage; authenticated visual states, mobile and dark-theme acceptance were not reviewed in this pass

The compatibility audit used the local NewAPI source at 142436bc0 and current official OpenAI/Claude cache documentation. Known aliases and nested locations are normalized without summing mirror fields or changing the meaning of original input totals. The common native conversion now preserves choice-level and timing cache reads before native cost calculation and across synchronous/asynchronous cache replay. Actual TTL evidence remains intact

New requests use token-v3 snapshots: dedicated duration prices take precedence, with a frozen generic write rate as fallback. Missing individual duration prices do not alone leave an otherwise valid request pending. Old token-v2 and flat snapshots retain their original rules, and existing bills are not recalculated. The billing receipt identifies a default rate used for an actual reported duration

The combined retail backend run passed 307 tests, followed by targeted regressions for positive canonical precedence, publication provenance and native serialization. A 175-test store/catalog/API/pricing run passed after the publication fix. Native tests covered 75 usage cases, 21 selected cache-cost/usage cases, 27 millisecond/timezone boundary cases and 6 complete native cost/log/cache-replay cases. Real PostgreSQL tests covered persistence, immutable price snapshots, publication flags, legacy aliases, aggregation, isolation and exact timestamp identity. Scoped frontend tests, production TypeScript and the webpack build passed. Regenerated OpenAPI types had no declaration delta

A running isolated proxy and scripted local upstreams passed 80 checks across nine requests: DeepSeek-style top-level hits, Zhipu top-level/input-detail counts, Moonshot choice usage, llama.cpp timing counts, mirrored aliases, creation-token aliases, mixed Responses TTLs without dedicated prices and Claude creation totals without duration details. Each request settled once. Charged usage, native spend-log cache counts and observations matched. The wallet and nine charge ledger rows reconciled to 4902950 nanos with no remaining reservation. These were local protocol fixtures, not paid vendor calls. The temporary database and proxy were deleted

That HTTP verification exposed native timestamp rounding: a serialized microsecond timestamp could round up in PostgreSQL while its observation was truncated. Native persistence now normalizes UTC and millisecond precision before serialization. The original duration calculation and strict call/account/start identity remain intact. No matching tolerance was added

The local service is healthy. Before/after fingerprints match for channels, native models, pool bindings, reference prices and wallets. The NewAPI checkout was read only and production was not changed. Existing frontend raw-fetch lint debt was not modified or suppressed; the changed presentation components added no lint errors

---

# Cache duration pricing and usage QA

final result: passed for the reviewed local desktop states on 2026-10-30

The public model pricing table now separates unspecified-duration writes, Claude 5m/1h writes and OpenAI 30m/12h writes. The reference-price editor supports those presets and custom integer-second durations. Bills, CSV, log rows/details, native cost breakdowns, model/channel detail statistics and Usage Overview/Model Activity retain the same duration categories. Historical records with reliable explicit fields can be separated; missing duration information remains unspecified

Before: C:/Users/26933/AppData/Local/Temp/codex-clipboard-61da58c2-f2c0-4df8-b02c-a63499f875c0.png

Claude pricing after: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/cache-claude-pricing-proof.png

OpenAI pricing after: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/cache-openai-pricing-proof.png

The actual anonymous local browser showed both duration tables with the existing navigation, typography, provider icons and table styling. Both pages had no captured browser warnings or errors. Authentication was preserved. Administrator editing, customer bills, exports, logs and statistics have scoped integration coverage; their authenticated visual states were not reviewed in this pass. Mobile and dark-theme acceptance were not performed

The combined retail backend run passed 282 tests. Separate PostgreSQL integrations verified price migration, immutable request snapshots, idempotent settlement, exact observation identity, historical duration parsing, aggregation without double counting, public model alias filtering and account isolation. Native targeted tests covered duration-aware cost calculation, protocol bridges and the Responses logging normalization regression. Scoped UI tests passed, followed by generated OpenAPI types, production TypeScript and the webpack build

A running isolated proxy with scripted local upstreams passed 14 HTTP checks. Anthropic stream/nonstream, OpenAI Chat and Responses requests each settled once and retained every duration subset in the native observation and model aggregates. Synthetic rates and a 0.65 pool discount produced charges of USD 0.002431 for each Anthropic request, USD 0.001846 for Chat and USD 0.002821 for Responses. Invalid price durations and an unpriced requested duration were rejected. These were local protocol fixtures, not paid provider calls. The temporary database and proxy were removed

The HTTP verification caught a native logging issue: assigning chat Usage into a typed Responses usage field caused serialization to retain only total_tokens. Callback responses now keep the native typed usage while the standard logging payload receives its separate normalized chat representation. A regression exercises the real success-helper normalization before the observation callback

The local deployment is healthy and the new aggregate route returns 401 without authentication. Before/after fingerprints matched for four channels, fifty-six native models, fifty-six pool bindings, reference prices and four wallets. No existing business rates, balances or bills were rewritten. Batch cost handling and Gemini storage token-hour lifecycle fees were not expanded

---

# Model overview integration card QA

final result: passed for the reviewed desktop state on 2026-10-30

The expanded Data sources block is replaced by an Integrate this model card. It provides the exact request model ID with copy, API example-format labels, an API-example action and a resource-pool action when pools exist. Published resources and account access remain separate. Unknown model modes do not gain guessed API formats

Source records, synchronization dates and conflict notes remain available behind a collapsed View data sources disclosure. They no longer occupy the main overview by default. The card retains the dashboard's existing borders, spacing, buttons and icons

Before: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-integration-before.png

After: C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-integration-card-proof.png

The actual local browser showed the collapsed state, expanded source fields on request and entered the correct API tab from the new action, preserving the selected model in the URL. The request code retained the exact model ID and credential placeholder. Browser error and warning collections were empty. Eleven scoped integration checks, scoped ESLint, production TypeScript and the webpack build passed. No backend, source synchronization, permission, price or billing change was required. Mobile and dark-theme visual acceptance were not performed

---

# Model performance, specification and benchmark charts QA

final result: passed for the reviewed desktop states on 2026-10-30

The directory now opens benchmark charts for the current filter, model details include Performance and Benchmarks tabs, and Compare models includes token-limit charts. Existing LiteLLM typography, provider icons, neutral borders and chart colors are retained. The final desktop captures use a 1280 x 720 viewport at density 1

| Evidence | Location |
| --- | --- |
| Filtered model benchmark explorer | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-benchmark-explorer-proof.png |
| Specification comparison and benchmark selector | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-specification-compare-proof.png |
| Live request traffic and success rate | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-site-traffic-final.png |
| Missing timing samples and measurement notes | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/model-site-performance-charts-proof.png |

The final browser review covered the filtered Benchmarks dialog, model comparison, public Performance tab and 24-hour/7-day range switching. Controls and labels remain readable, dialogs scroll, and sparse success samples remain visible on a fixed 0-100% scale. Browser error and warning collections were empty. Mobile and dark-theme acceptance were not performed

Saved source snapshots provide 620 benchmark observations across 35 of the 44 configured public models at verification time. Each result retains source and protocol settings. Different sources, versions, harnesses, datasets and variants are separate choices. Index points and Elo keep their original units, unknown metrics remain a raw table, and declared capabilities never become fabricated evaluation scores. Specification charts compare declared token limits and do not imply quality rankings

Live site statistics returned seven requests for the reviewed model, three successful and four failed, with the same totals in the trend buckets. Public charts aggregate current public resources, while account charts require current pool/model authorization and include only the authenticated user's requests. Missing samples stay unknown. The API excludes credentials, user identifiers, upstream addresses, raw errors and financial fields

Verification exposed two timing issues and both were fixed before delivery. Native completionStartTime may be a control event or an end-time fallback, so it is never used as verified TTFT. The original logging start can also differ from the persisted native log start; the observer uses the native success callback's exact start rather than a time tolerance. Existing unverified logs therefore have no TTFT or speed sample

An isolated running LiteLLM proxy and local Anthropic SSE server passed seven checks. Opening and heartbeat events preceded actual text; the collector recorded the later model output and left every response chunk unchanged. PostgreSQL observations joined the native log by call ID, account and exact millisecond start, producing a verified TTFT near 496 ms and an observed output-speed sample. This was a local protocol fixture, not a paid vendor call. Other recognized protocol parsers have unit coverage

The combined backend run passed 85 checks. A persisted PostgreSQL observation regression and 13 independent SQL cases passed. The final combined UI run passed 29 checks, followed by 10 benchmark/explorer checks after coverage-selection refinements and 10 shared-chart checks after the fixed percentage axis. Three pure trend-data tests also passed. Production TypeScript, scoped Python/frontend lint and the final webpack build passed

The local service is healthy. Before/after fingerprints for live channels, native models, pool bindings, price configuration and wallets matched exactly. Source refresh imported benchmark metadata without changing reference prices or billing. No paid inference request was made. Observation writes and aggregate queries are bounded; historical missing measurements remain visible as unavailable

---

# Dual-source model metadata QA

final result: passed for the reviewed desktop states

The public and personal catalogs retain the LiteLLM shell, provider icons, neutral card borders, typography and existing price controls. Cards now use a sourced description and display attribution with the retrieval date. Details show field-level sources, total context separately from maximum input, and differing source declarations. Missing descriptions use a short unpublished state

| Evidence | Location |
| --- | --- |
| Previous public layout and issue supplied by the user | C:/Users/26933/AppData/Local/Temp/codex-clipboard-3c18b5ae-aaad-4718-9bbe-e22bb4683156.png |
| Updated public list | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-metadata-list-proof.png |
| Updated detail header and limits | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-metadata-detail-final.png |
| Field attribution section | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-metadata-detail-proof.png |
| Actual administrator sync completed | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-metadata-admin-proof.png |

Implementation captures use a 1280 x 720 viewport at density 1. The user's issue capture includes browser chrome and a different viewport; this review evaluates the existing product style and the changed data behavior rather than pixel identity. Fonts, spacing, provider assets, readable muted copy, source attribution and narrow text wrapping were reviewed in the actual browser. The detail branch gained the same padding as the list after the first browser pass showed it flush to the viewport edge

The administrator's Sync now button was exercised against both real public data APIs. Saved snapshots contain 607 models.dev and 456 OpenRouter records at verification time. The 44 configured public model IDs remain unchanged; 42 use models.dev and 39 also use OpenRouter via exact IDs or declared canonical mappings. Two unmatched aliases retain existing metadata. Source API descriptions may themselves be abbreviated, and the catalog does not invent missing text

Before/after fingerprints of live channels, native model configurations, bindings, price configuration and wallets matched exactly. All 44 public model reference rates and resource offers also matched. Anonymous administrator endpoints returned 401. ASGI authorization regressions reject ordinary users and read-only administrators for both source operations. No paid inference calls were made

Backend catalog, enrichment, normalization, failure retention and authorization tests passed: 42. An isolated PostgreSQL fixture passed 7 persistence, partial-failure, lock and retention checks and was removed. Targeted frontend metadata, source-admin, comparison, personal-catalog, identity and page-permission checks passed, including the HTTP 200 syncing-in-progress state. Production TypeScript, scoped lint and the final webpack build passed. Browser warning/error collections were empty

Automatic refresh runs every six hours, serves saved snapshots during source failures and imports no external price or performance fields. Declared capabilities remain reference data, with channel support explained in the UI. Mobile and dark-theme visual acceptance were not performed. The existing horizontal model-management tab strip remains scrollable for the Catalog data tab

---

# Channel connection editor QA

final result: passed for the desktop states reviewed

The existing channel editor now includes an Upstream connection section for changing the API base URL and replacing the API key. It retains the current LiteLLM modal, form controls, typography, neutral borders and dark Save action. The resource pool control shows the readable name and draft state.

| Evidence | Location |
| --- | --- |
| Existing editor reference supplied by the user | C:/Users/26933/AppData/Local/Temp/codex-clipboard-a70ab33a-0536-4eba-8e84-995e1b36cb70.png |
| URL and key behavior reference supplied by the user | C:/Users/26933/AppData/Local/Temp/codex-clipboard-35db4ed3-f741-41a3-a2b1-1435b68d9bc9.png |
| Final implementation | http://127.0.0.1:4000/ui/channels/ |
| Final capture | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/channel-connection-editor-proof.png |

The final capture uses a 1280 x 720 viewport and screenshot at density 1. The user references have different image dimensions and include partial product views, so this is a functional adaptation within the existing dashboard rather than a pixel clone. The final screenshot and URL/key reference were opened together for comparison. Labels, helper text and input values remain readable; controls align within the modal. Existing provider assets and neutral form styling are retained. No decorative assets were introduced.

The actual local editor loaded the safe connection summary for 14 selected deployments. Enabling Change API base URL made the URL editable. The new key field started empty and no saved key or fragment was returned. Discard restored the local draft; scrolling exposed the existing Save channel and Cancel buttons without clipping. Cancel closed the editor without a write. The closed pool selector showed Anthropic (draft). Browser warning/error collection returned an empty list.

Targeted backend coverage passed: 57 connection/channel/store checks, 3 unsafe-existing-URL regressions and 6 non-API-key authentication regressions. Targeted frontend coverage passed: 18 tests. Production TypeScript checking and the webpack production build passed. An isolated PostgreSQL fixture passed 17 checks, including compensation and concurrent-edit protection. An isolated running LiteLLM proxy with local Anthropic mocks passed 12 checks, including immediate routing after URL/key replacement, blank-key preservation and anonymous/ordinary-user denial. These were local mocks, not real vendor authentication calls.

The updated local service is healthy. Before/after fingerprints of all 4 live channels, 56 native model configurations and 56 pool bindings matched exactly. No paid upstream call or live connection edit was required. Shared named credentials, static models and explicit alternate authentication without an API key remain read-only with an explanation. Mobile and dark-theme visual acceptance were not performed.

No actionable desktop P0, P1 or P2 finding remains in the reviewed states. The existing vertically scrolling modal is preserved; the save action is reachable after the deployment list.

---

# Channel analytics design QA

final result: passed

## Current implementation and evidence

The channel analytics extension follows the existing LiteLLM Usage page's typography, neutral borders, statistic cells, native chart colors, table controls and dashboard shell. It adds an operational channel view and independent channel details rather than reproducing the Overview's model ranking content

All captures use a 1280 x 720 CSS viewport and 1280 x 720 screenshot pixels at density 1, without browser chrome. The source and final implementation captures were opened together in the same comparison input. These are different functional views, so the comparison evaluates the existing product language and hierarchy, rather than pixel identity

| Evidence | Location |
| --- | --- |
| Existing Usage visual reference | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/channel-usage-reference.png |
| Final channel summary | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/channel-activity-proof.png |
| Final detail header and rolling rates | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/channel-detail-proof.png |
| Focused financial and performance cells | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/channel-stats-proof.png |
| Final sparse measurement charts | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/channel-performance-proof.png |

## Visual and interaction findings

No actionable P0, P1 or P2 findings remain in the desktop states reviewed. Typography and hierarchy follow the existing UI. Statistic grids use the same muted labels, clear numeric values and rounded borders. The detail intentionally contains more cells and two chart columns to expose channel measurements. Tables retain horizontal scrolling and configurable columns for the larger metric set. Colors use existing blue, cyan, green and red chart tokens. Existing branding and library icons remain sharp; no replacement art was introduced. Copy distinguishes configuration status, gateway spend, missing samples and near-real-time persisted observations

The full screenshots and the focused statistics/chart captures make the control labels and metric cells readable. Search by resource pool, clearing search, showing the p95 column, opening channel details, switching Models and Errors, pausing refresh, changing from 24 hours to 7 days and opening an actual request-log detail were exercised in the local browser. URL state retained the exact channel and selected window. The final fresh tab returned no console warnings or errors

| Earlier finding | Fix and final evidence |
| --- | --- |
| P2: the new detail route highlighted Home | Added Channels selection and the proper gateway breadcrumb, covered by navigation regression tests and the final detail capture |
| P2: sparse cost or timing observations could become invisible without a line segment | Enabled optional sample dots for sparse channel measurement charts, retaining null gaps. The final performance capture shows isolated cost observations. A chart regression proves one isolated sample remains visible without drawing a segment through missing data |
| Hidden Overview charts emitted zero-size warnings when entering Channel Activity | Existing non-channel panels unmount while the channel tab is selected. Original Usage tab behavior remains unchanged, and the final fresh channel tab has no console warnings |

## Data and permission validation

Targeted backend tests and native spend-log projection tests pass. Independent PostgreSQL fixtures passed 17 cases, including percentiles, explicit streaming, retry helper accounting, model isolation and timezone boundaries. A real local API versus independent SQL reconciliation passed 48 checks over identical response windows. Anonymous and ordinary users cannot read channel analytics. The fixture database was removed, with no main-database business record changes or paid upstream calls

The production TypeScript check and final webpack build pass. Targeted UI, Usage, navigation and chart tests pass. ESLint has no errors; existing legacy component and test warnings remain outside the new metrics module. The final local gateway is healthy

Only the desktop light-theme viewport was visually checked. Mobile and dark-theme screenshots remain unverified. Historical logs without exact deployment IDs remain unattributed, and logs without a trusted stream flag cannot supply TTFT. The UI exposes those limitations rather than estimating measurements. Native spend is not supplier invoice cost or discounted wallet revenue

## Previous model discovery QA

# Model discovery design QA

final result: passed

## Scope and evidence

The public discovery page follows OpenRouter's model exploration structure while retaining LiteLLM's existing visual system. The authenticated sidebar page remains a personal catalog. This is a functional adaptation, rather than a pixel-identical clone or a claim to reproduce OpenRouter's measured performance data.

| Evidence | Location |
| --- | --- |
| Source visual truth | https://openrouter.ai/models |
| Source capture | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-openrouter-reference.png |
| Public implementation | http://127.0.0.1:4000/ui/models/ |
| Public capture | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-public-proof.png |
| Personal implementation | http://localhost:4000/ui/model-catalog/ |
| Personal capture | C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-personal-proof.png |

All three captures use a 1280 x 720 CSS viewport and 1280 x 720 screenshot pixels, with density 1. No rescaling or browser chrome normalization was needed. The source and public implementation represent the light-theme, default unfiltered list state without an account-specific view. They have different real model datasets. The personal capture is intentionally authenticated and contains only account-authorized models.

The source and both implementation images were opened together in the same comparison input for the final review. The complete captures make the typography, controls, icons and card contents readable, so a separate enlarged region was unnecessary for this pass.

## Findings

No actionable P0, P1 or P2 findings remain in the desktop states reviewed.

| Required surface | Observed result |
| --- | --- |
| Fonts and typography | LiteLLM's existing sans-serif typography, bold model headings and muted metadata remain consistent with its dashboard. Exact request IDs use a smaller monospace line. The visible headings, form labels and card text wrap without collisions. The reference's font metrics are not copied exactly. |
| Spacing and layout rhythm | Public navigation, a filter rail, search and sorting controls, mode tabs and model cards preserve the reference's exploration hierarchy. Cards deliberately include an exact request ID and capability/resource badges and are taller than the reference. The personal page retains the existing dashboard sidebar and has less horizontal content space. Persistent desktop controls remain visible. |
| Colors and tokens | Existing LiteLLM blue branding, neutral borders, white surfaces and dark primary actions are preserved. The reference's purple accent is intentionally not introduced into the existing product. Account availability is green only in the personal view. |
| Image quality and assets | LiteLLM's existing brand image and local provider assets are used. Anthropic, OpenAI and Google model identities are recognizable. No handcrafted substitute for a provider logo or generated decorative image was added. |
| Copy and content | Public discovery describes all configured request models, while the personal heading says My models. Reference prices are labeled From and explain additional price tiers. Input limits are labeled input context. Exact API IDs are retained. Missing descriptions use declared modalities and capabilities, without invented benchmarks or use cases. |

## Comparison and correction history

| Earlier issue | Correction | Post-fix evidence |
| --- | --- | --- |
| P2: select controls displayed raw stored values such as name, 0 and -1 instead of readable labels | Registered option labels and corrected control sizing | Final public capture shows Name A-Z; context and price controls use readable labels |
| P2: models with additional billing tiers lost otherwise valid base reference prices | Separated base reference-rate display from strict billable customer-quote validation | Final public capture shows base input/output prices; live reference checks and targeted fallback tests pass |
| P2: public comparison footer implied resource offers for models without any public offers | Changed the explanation to base catalog reference pricing | Public comparison was opened after the copy correction; no personal quote or availability row appears |

The earlier public capture is saved at C:/Users/26933/Documents/Codex/2026-10-29/https-github-com-quantumnous-new-api/work/catalog-public-before-fix.png. The final implementation was recaptured after the successful production build and compared against the source in the combined review described above.

## Browser interaction and permission checks

Search, author filtering, list/table switching, model details, the API tab, two-model comparison and returning to the list were exercised in the actual local browser. Searching gpt-6.1 produced two models; the OpenAI filter produced eight models. The detail recipe retained the selected exact request ID. Public browser warning/error collection returned an empty list.

Anonymous public HTML returns HTTP 200 without a login redirect. The public API returns 44 models and has the same payload when signed in. The ordinary test account's personal catalog and inference model list both contain 12 models. Private endpoints require authentication, customer quotes agree with existing discount calculations, and supplier fields are absent. No supplier inference call or wallet mutation was needed to verify model discovery.

Backend targeted tests: 130 passed. Final catalog frontend suite: 9 files, 65 tests passed. Later focused reference and visual-control checks passed. Production TypeScript checking and the webpack production build completed successfully. The local gateway is healthy.

## Residual gaps and follow-up polish

Mobile viewport and dark-theme screenshots were not separately captured in this browser session. Responsive filter-drawer behavior is implemented, but this report does not claim mobile visual acceptance. Current model metadata has no authored descriptions and two models lack declared context limits, so the interface uses factual summaries and unknown values. Rich editorial descriptions and measured throughput, latency or benchmark comparisons require additional maintained data.

P3: replace repeated metadata summary phrasing with editorial descriptions when those descriptions are available. This does not block the separate public and personal experiences.

## Implementation checklist

- Public top-navigation catalog is independent of dashboard authentication.
- Sidebar catalog remains account-scoped.
- Public discovery does not expose restricted resource identities or customer prices.
- Search, filters, comparison, details and exact API recipes work in the browser.
- Production build, targeted tests and read-only runtime verification pass.
- No customer access grants, balances or upstream channel settings were changed.
