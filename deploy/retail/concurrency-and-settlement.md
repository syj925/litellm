# Retail request capacity and settlement

The retail runtime reuses PostgreSQL connections and separates preflight, terminal usage persistence and diagnostic writes. A process admits a bounded number of inference requests before reading their bodies or preparing upstream calls. When capacity, the preflight connection queue, native logging, or the durable settlement backlog is overloaded, new requests receive a retryable 503. Existing requests continue through their terminal handling using a dedicated fact-persistence pool

## Request path

Authentication and routing still enforce current account permissions. Billing reads the model price, wallet and discount in one transaction, freezes the selected price, and atomically reserves funds. The reservation transaction remains authoritative across processes. Local account gates are acquired before a financial write borrows a database connection, so a hot account cannot fill the pool with local row-lock waiters

Ordinary streaming chunks do not write financial rows. Before a successful terminal chunk or EOF reaches the client, the gateway commits normalized reported usage and the amount derived from the frozen price to `retail_settlement_facts`. Model content, tool arguments and credentials are not part of that record. Local token estimates cannot substitute for missing provider usage

A bounded settlement worker claims facts with `FOR UPDATE SKIP LOCKED` and a lease. It uses the existing atomic wallet, ledger and reservation settlement operation. Retries, multiple workers, process restarts and late duplicate callbacks do not create another charge. Failed attempts retain their facts for retry. A manual resolution that conflicts with a fact is recorded as a conflict for review

Missing or inconsistent reported usage stays unknown and retains its financial hold. A request's active lease is separate from that hold. Completed or disconnected requests stop renewing their active lease; a crashed process cannot permanently consume the account's concurrent-request slots. Unknown usage is not automatically refunded or charged as zero

## Resource budgets

These limits are per process. Include all application processes, Prisma connections, workers and administrative tools when sizing the PostgreSQL connection budget

| Setting | Default | Meaning |
| --- | ---: | --- |
| `RETAIL_MAX_INFLIGHT` | 512 | Inference requests admitted by the process |
| `RETAIL_MAX_PARALLEL` | 10 | Active wallet requests per account, enforced in the reservation transaction |
| `RETAIL_DB_MAX_CONNECTIONS` | 16 | Reused financial connections |
| `RETAIL_DB_ADMISSION_WAITING` | 16 | Financial waiters before admission sheds new requests |
| `RETAIL_DB_DIAGNOSTIC_CONNECTIONS` | 3 | Separate trace, model and cache observation connections |
| `RETAIL_DB_CRITICAL_CONNECTIONS` | 4 | Dedicated terminal fact persistence connections |
| `RETAIL_DB_CRITICAL_MAX_WAITING` | in-flight limit + 32 | Terminal fact waiters, isolated from new preflight work |
| `RETAIL_DB_CRITICAL_BORROW_TIMEOUT_SECONDS` | 10 | Terminal fact connection acquisition deadline |
| `RETAIL_DB_MAX_WAITING` | 64 | Financial connection waiters |
| `RETAIL_DB_DIAGNOSTIC_MAX_WAITING` | 12 | Diagnostic connection waiters |
| `RETAIL_DB_BORROW_TIMEOUT_SECONDS` | 5 | Connection acquisition deadline |
| `RETAIL_DB_ACCOUNT_MAX_WAITING` | 64 | Local waiters behind one account write gate |
| `RETAIL_DB_ACCOUNT_TIMEOUT_SECONDS` | 5 | Local account gate deadline |
| `RETAIL_DB_ACCOUNT_GATES` | 1024 | Active local account gate entries, removed when idle |
| `RETAIL_DB_STATEMENT_TIMEOUT_MS` | 5000 | Server statement deadline |
| `RETAIL_DB_LOCK_TIMEOUT_MS` | 1000 | Server row or advisory lock deadline |
| `RETAIL_DB_IDLE_TRANSACTION_TIMEOUT_MS` | 10000 | Server idle transaction deadline |
| `RETAIL_PREFLIGHT_TIMEOUT_SECONDS` | 10 | Total asynchronous billing preparation deadline |
| `RETAIL_ACTIVE_LEASE_SECONDS` | 120 | Active request lease, renewed while the request is live |
| `RETAIL_SETTLEMENT_CONCURRENCY` | 4 | Settlement workers per process |
| `RETAIL_SETTLEMENT_LEASE_SECONDS` | 60 | Crash recovery claim lease |
| `RETAIL_SETTLEMENT_MAX_BACKLOG` | 1000 | Known unprocessed facts before admission sheds new load |

Connection acquisition checks stale connections before use. Aborted statements and cancelled transactions roll back before the connection returns to the pool. The application does not automatically replay a write whose outcome is unknown

The trace writer holds at most 256 pending identities plus its two active writes, with a size bound per record. Trace, model and cache observations use the diagnostic pool. Cleanup runs in separate, short batches and skips locked rows. Native logging executes all callbacks under its configured semaphore and uses one FIFO overflow pump instead of creating a task for every retry. Native callbacks can hold arbitrary in-memory coroutine state, so preserving them without blocking is not a hard memory bound by itself; admission stops new inference work while that queue is overloaded

## Operations

`GET /retail/admin/runtime` returns per-process pool and queue counters, admission state and settlement pressure, with a process ID so a collector can distinguish workers. Full proxy administrators can read it; ordinary users and administrator viewers cannot. It does not query or expose customer requests, upstream identities or credentials

Administrator trace views can recover an already calculated native provider-cost observation when the wallet snapshot has none. This is a bounded, read-only lookup in the same database and requires the exact server reservation ID, user and native call ID. Missing, ambiguous, zero-default or unlinked legacy records remain unknown. This lookup does not recalculate customer charges or run in the forwarding path

The compose override builds a small dependency layer over the pinned upstream image. Its `psycopg-pool` wheel is version pinned and hash checked. The source overlays include the native logging and streaming paths that the retail runtime uses. Do not activate the source overlays against the old image without installing that dependency

The local override defaults to two application workers, configurable with `RETAIL_SERVER_WORKERS`. Prisma is capped at ten connections per worker, separately from the pure PostgreSQL URL used by the three retail pools. Two workers therefore have a maximum configured database budget of 66 connections before other tools and services; a server with a smaller budget must use fewer connections or processes. Critical fact-persistence connections are established before readiness, so a missing database budget fails startup instead of first failing at the end of a live stream

On shutdown, stop admitting new work, allow native in-flight requests and callbacks to drain, stop the settlement worker, flush diagnostics, and close all three database pools. The supplied override allows 120 seconds for the container and configures a 90-second native graceful drain. Durable facts remain available if the process is killed before settlement completes

Capacity tests should report arrival rate, active streams, body sizes, stream lifetime, retries and errors separately. A closed-loop short mock test is useful for comparing changes, but does not validate thousands of long-running production streams. Check funds held, settled facts, ledger uniqueness and queue drain in addition to client EOF
