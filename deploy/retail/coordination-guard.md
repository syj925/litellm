# Coordination Redis admission guard

`RETAIL_REQUIRE_COORDINATION_REDIS=true` enables a separate admission guard for requests carrying gateway credentials. The library default is `false`; enabling the flag requires the native coordination Redis configuration to be available

Each process checks coordination Redis in the background, with a 500 ms interval and a 500 ms probe deadline. Credential-bearing HTTP requests use only the current local health snapshot. They do not make a Redis or database call for this guard. Unknown, failed or more than two-second-old health snapshots return a private `503` response with `Retry-After: 1`. Under normal scheduling a failed probe closes admission within one probe cycle, at most one second with the default settings. A stalled monitor closes admission when its last successful snapshot exceeds two seconds

The guard detects the native authorization headers, configured custom LiteLLM key header, the `token` session cookie, key query parameters and OpenAI WebSocket key subprotocol. New authenticated WebSocket handshakes close with code `1013` while unavailable. Already admitted requests and streams continue; this guard does not retroactively cancel them or promise instantaneous revocation

Exact public site pages, static assets, password login and basic liveness remain accessible. Exact `GET/HEAD /health/readiness` returns a safe `503` while coordination is unknown, failed or stale, and delegates to native readiness after recovery. Detailed readiness, wallet, inference, native key management and all credential-bearing retail APIs remain guarded. Other anonymous requests continue to their normal native authentication and authorization handlers. There is no broad `/retail` or `/health` bypass

Before reopening admission after startup, a failed probe or an expired snapshot, each process clears only its native in-memory authorization-object and key-object partitions. It never flushes Redis or the separate spend, budget-counter, rate-limit or wallet stores. This prevents an authorization entry missed during a Redis outage from surviving local recovery

Each request also captures an opaque generation in its async context. Failure and recovery replace that generation before clearing authorization objects. Native authorization-cache setters accept writes only from the current generation while coordination remains healthy. An old request or its inherited child task cannot refill the cleared authorization cache after recovery. Background work without a request generation skips these cache writes in strict mode. Deletes and separate financial counters are unaffected

Strict mode currently requires `litellm_settings.enable_redis_auth_cache=false`. Redis-backed authorization objects can retain a missed update across an outage, and are not supported by the local recovery reset. Startup rejects that configuration; if attached later, the guard closes admission. Shared coordination Redis for invalidation, rate limits and counters remains supported

Strict mode also rejects `DATABASE_URL_READ_REPLICA` and an active native `RoutingPrismaWrapper`. A lagging replica could repopulate a revoked credential after the local reset, so read-replica authentication is outside this mode's recovery guarantee. Use the native writer for this first version

The optional timing settings are `RETAIL_COORDINATION_HEALTH_INTERVAL_SECONDS`, `RETAIL_COORDINATION_HEALTH_TIMEOUT_SECONDS` and `RETAIL_COORDINATION_HEALTH_MAX_AGE_SECONDS`. Interval and timeout must be between 0.05 and 0.5 seconds; maximum age must cover one probe cycle and cannot exceed two seconds. Invalid or nonfinite settings are rejected. Safe status is available through `coordination_guard_status()`; it contains no credentials, Redis addresses or request content

Integration uses the pure ASGI `CoordinationGuardMiddleware`, placed outside request-body parsing and authentication. Call `start_coordination_guard()` after native Redis configuration is initialized, independently of the management audit flag. Call `close_coordination_guard()` before native Redis shutdown
