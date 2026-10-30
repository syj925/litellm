# Channel activity and performance

Proxy administrators can open Channel Activity from Usage or the Channels page. Clicking a named channel opens its dedicated details at `/ui/channel-details/?channel=<id>`. The detail has Overview, Models and Errors tabs. The server authorizes both analytics endpoints; hiding a tab is not the access boundary

| Endpoint | Response |
| --- | --- |
| `GET /retail/admin/channels/metrics?hours=24` | One batch of channel summaries, rolling rates, data coverage and measurement notes |
| `GET /retail/admin/channels/{id}/metrics?hours=24` | Channel summary, model breakdown, time buckets, error categories and recent safe errors |

Supported windows are 1 hour, 6 hours, 24 hours, 7 days and 30 days. The UI refreshes every 10 seconds while visible, with pause and manual refresh controls. SQL results can be reused for 8 seconds. Errors leave a clearly marked previous snapshot or an error state, rather than a successful-looking zero

## Measurement definitions

| Measurement | Definition |
| --- | --- |
| Channel attribution | Exact native deployment `model_id` joined to current channel membership. Shared public model names never determine the channel |
| Requests and failure rate | Final persisted gateway requests. Generated billed-attempt helper rows are excluded from the denominator. Unknown terminal statuses prevent a complete failure-rate claim |
| RPM | Logged final requests whose start time falls within the last 60 seconds |
| TPM and USD per minute | Recorded tokens and native spend for logs whose start time falls within the last 60 seconds, including recorded billed attempts |
| Native gateway USD | The native `LiteLLM_SpendLogs.spend` accounting basis. It is separate from discounted wallet charges, supplier invoices and profit |
| Latency | Recorded gateway request duration, with average, p50, p95 and sample count |
| TTFT | Successful explicitly observed streaming requests with a valid completion-start timestamp. Response-cache hits are excluded. Average, p50, p95 and sample counts are available |
| Prompt cache tokens | Recorded cache-read/cache-write usage. Missing fields remain unknown |
| Gateway cache-hit rate | Native response-cache hits, distinct from provider prompt caching |
| Errors | Final request errors and upstream error events are separate. Upstream events can include retries for one request |

All windows are half-open and use request start time. A long request appears only after completion and asynchronous log persistence, so these rates are near-real-time log observations rather than active connections, in-flight concurrency, configured rate limits or active health probes

`observed_stream` is stamped during native spend-log projection from a strict boolean call argument or the trusted standard logging payload. A client-supplied metadata value cannot override this measurement. Historical logs without that flag cannot be retroactively classified as streaming from timestamps alone

## Data coverage and limits

The coverage panel reports gateway-wide log rows, attributed requests, unattributed requests and deployment-ID coverage for the selected period. Pre-dispatch rejections, logs missing a deployment ID and removed deployments are not assigned to a channel by guessing. Moving an existing deployment between channels can change historical attribution because this version uses current membership rather than a historical ownership ledger

Model rows include rolling rates, requests, failures, native spend, input/output/cache/reasoning tokens and measured timing. Table column controls expose additional measurements. Trend buckets distinguish no observations from missing timing or financial samples. Unknown values appear as unavailable

Recent errors contain generated summaries, validated HTTP codes and known exception categories. Request bodies, upstream credentials, raw exception strings and upstream HTML are not returned by these analytics endpoints. Only final request entries link to an existing request-log detail; upstream event IDs remain distinct

Queries aggregate in PostgreSQL over bounded time windows and use the existing start-time indexes. A short singleflight cache avoids duplicate simultaneous polling scans, and each statement has a 15-second timeout. No new monitoring service or per-request database lookup was added. Large installations still need capacity testing and may need additional indexes or materialized rollups according to their actual log volume
