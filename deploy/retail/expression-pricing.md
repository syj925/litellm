# Expression pricing

Model Pricing supports fixed token rates and versioned expression rules. Administrators select an existing model, edit its expression and timezone, simulate reported usage, and save the rule. Switching back to fixed rates clears the expression. Viewing the catalog or running a simulation never saves prices

The engine uses a bounded parser and Decimal arithmetic. It does not run Python, JavaScript, shell commands, imports or property access. It supports arithmetic, comparisons, `&&`, `||`, `!`, parentheses and `condition ? value : other`. Token coefficients are USD per million tokens. `p * 2 + c * 8` charges USD 2 per million ordinary input tokens and USD 8 per million ordinary output tokens. `fixed(0.04)` is USD 0.04 per successful request

These are example rates, not vendor price declarations

| Input | Meaning |
| --- | --- |
| `len` | Complete reported input context, including cache reads and writes |
| `p`, `c` | Ordinary input and output, excluding separately referenced categories |
| `cr`, `cc` | Cache reads and writes, excluding separately referenced cache categories |
| `cc5m`, `cc30m`, `cc1h`, `cc12h` | Explicitly reported cache write duration subsets |
| `cache_write("750")` | Explicitly reported writes with a custom duration in seconds |
| `img`, `img_cr`, `img_o` | Uncached image input, cached image input and image output tokens |
| `ai`, `ao`, `reasoning` | Audio input, audio output and reasoning tokens |
| `u("images")` | Actual returned image count |
| `u("audio_seconds")` | Reported audio seconds, when the endpoint supplies this quantity |
| `u("web_search_requests")` | Reported search count, without summing mirrored aliases |

Do not subtract these partitions again in the formula. `cc * 2.5 + cc1h * 4` already excludes the reported one-hour subset from `cc`. When an upstream reports only a write total, unclassified writes remain in `cc`; no duration is invented. Missing required modality counts or other quantities remain unknown and keep a request pending rather than producing a zero charge

`u()` reads numeric usage facts, not arbitrary request fields. Its keys require maximum quantities in `usage_bounds` so the wallet can reserve enough funds. `fixed(u("images") * 0.04)` bills actual returned images, independently of the requested count. Image token pricing and per-image alternatives are never added together. `reported("tokens")` selects the token alternative when valid positive token usage is available

## Context, service and time

`len > 200000 ? tier("long context", p * 4 + c * 16) : tier("standard", p * 2 + c * 8)` selects a rate using complete input length. Cache hits do not shrink the context used for this decision. `tier()` returns its numeric value and records its label on the bill

`param("service_tier", "default") == "priority" ? tier("priority", p * 4 + c * 16) : tier("standard", p * 2 + c * 8)` selects a price from a safe request parameter. Supported scalar options include service tier, quality, size and resolution. Prompts, headers and credentials cannot be read. Only referenced permitted options are frozen privately in the reservation

`weekday() != 0 && weekday() != 6 && hour() >= 9 && hour() < 18 ? tier("weekday peak", p * 4 + c * 16) : tier("off peak", p * 2 + c * 8)` defines a weekday peak period. `weekday()` uses Sunday 0 through Saturday 6. `hour`, `minute`, `weekday`, `month`, `day` and `date` use the configured IANA timezone or an explicit timezone argument. `date()` returns `YYYY-MM-DD`, allowing explicit holiday exceptions. Unknown timezones are rejected; holiday dates are configured rather than guessed from weekdays

Request start time, timezone, expression, price version and account discount are frozen before the upstream call. A request finishing after a peak boundary or price change still uses its original snapshot. Settlement reevaluates that rule with actual quantities. Later requests use the new version

## Reservation and receipts

Reservation uses conservative upper bounds across expression branches and configured non-token quantities. Opaque inputs use the catalog context limit or explicit `usage_bounds.input_tokens`; media output uses its catalog capacity or `usage_bounds.output_tokens`. Unknown capacities use the existing one-million-token fallback. Quantities beyond a frozen bound stay pending

Settlement applies the saved customer, plan or pool discount once and rounds up to nano-USD once. Receipt and CSV retain expression text and hash, frozen time and timezone, matched tiers, quantities actually read, undiscounted amount, discount, final charge and rounding adjustment. They exclude private context. Repeated callbacks remain idempotent

Existing fixed prices and historical flat-token-v1, token-v2 and token-v3 snapshots retain their original rules. The schema adds a nullable expression policy without rewriting existing rows or bills. Channel procurement cost remains separate

Known token, context, service, modality and cache fields translate without flattening their dimensions. Unsupported or contradictory fields require explicit review. Original values and units remain available. Catalog imports require explicit administrator selection

Automatic catalog translation also preserves supported reported search counts. OpenAI search fees require equal configured context-size prices; Anthropic uses its native medium search fee. Regional processing multipliers, shared daily search allowances and unrecognized pricing dimensions require manual pricing instead of a partially translated active rule. A model's availability in the catalog does not establish that it already has a complete billable price

Chat, text completion, Responses, Anthropic Messages and embeddings share the expression engine. Image generation/editing, speech and transcription require an expression and its reported quantities. Priced server search requires an explicit search quantity. Missing facts never fall back to inactive fixed columns

Background jobs, asynchronous Batch submission and realtime sessions retain existing endpoint restrictions. A Batch service-tier price can be represented by a rule; this does not create an asynchronous Batch accounting lifecycle

`POST /retail/admin/pricing-preview` is an administrator-only read simulator. It accepts `expression_policy`, synthetic `facts`, safe `request` pricing options, an aware `started_at` timestamp and a discount. It uses the settlement compiler and evaluator and returns the nano-rounded amount and trace. Synthetic data does not verify which quantities a vendor reports
