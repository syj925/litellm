# Cache duration pricing and usage

Administrators configure reference prices in Customer wallets. The editor supports cache write prices for 5 minutes, 30 minutes, 1 hour, 12 hours and custom durations in seconds. Prices shown in the editor and public catalog are USD per million tokens. The existing generic write price is explicitly labeled unspecified duration

`POST /retail/admin/model-prices` accepts `cache_write_tiers` as a map from canonical positive integer seconds to decimal USD per million tokens. For example, `{"300":"2.50","3600":"4.00","750":"0"}` defines two standard durations and a free custom duration. Up to sixteen entries are allowed, with durations from 1 to 31536000 seconds. Duplicate durations, non-finite prices, negative prices and invalid duration keys are rejected

Stored prices use USD per token. New requests freeze a token-v2 price snapshot, including the duration map and the account's pool/model discount. The discount applies to every category. Updating a reference price cannot change a request's frozen price or an existing bill. Legacy flat-token-v1 reservations keep their original settlement rules

The normalized cache write total includes its duration subsets. Billing charges each known subset once and charges the remainder with the unspecified-duration rate. Mirrored provider fields are aliases, not additional usage. Subsets larger than the total are invalid. OpenAI input counts include cached input and writes, so ordinary input is calculated after subtracting both. Anthropic input counts already exclude those categories

Requested durations without configured prices are rejected before dispatch. If an upstream unexpectedly reports an unpriced duration or contradictory usage, settlement stays pending with a safe billing error rather than guessing a price. The reservation remains visible for reconciliation. Line costs retain decimal precision, and the final charge is rounded once; the saved rounding adjustment reconciles the line costs to the charged amount

The bill detail and CSV include duration quantities, saved prices and discounted costs. Logs expose cache reads, write totals, duration subsets and unspecified writes. Native upstream cost details retain their own duration subsets and remain separate from customer charges. A provider that reports only a combined cache read count does not acquire invented read-duration buckets

Model/channel detail statistics and Usage Overview/Model Activity include duration summaries. The Usage card follows the selected date, user and model filters. Its `/retail/cache-usage` endpoint uses inclusive start and exclusive end timestamps, requires explicit timezone offsets and limits queries to 31 days. Ordinary users are restricted to themselves; global queries require an administrator. Customer model filters use public aliases. Unsupported or unauthorized filters fail explicitly without falling back to global data

New observations join native spend rows by call ID, account and the exact millisecond start time. Writes are bounded to two concurrent tasks, sixty-four queued tasks and a two-second deadline. Aggregate queries have a three-second limit. Missing observations remain unknown. Retry helper rows are excluded. Historical logs with explicit valid provider duration fields can be separated; records with only a write total remain unspecified. No migration rewrites historical bills or usage

The local deployment override mounts the changed native usage, cost, transformation and logging modules in addition to the retail package. Apply it with the compose file used for this installation, then rebuild the dashboard. Restarting a container without the new mounts is insufficient

The native Batch cost path and Gemini cache storage token-hour lifecycle charges are outside this change. Prepaid retail Batch and background requests remain unsupported. Protocol fixtures verify the recognized usage structures, but do not establish whether every configured upstream implements those fields
