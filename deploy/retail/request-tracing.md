# Request tracing

Administrators can expand Request trace in a model call's log detail. New requests retain an entry timestamp, body receipt, JSON parsing, authentication, routing, wallet reservation and settlement phases, upstream HTTP attempts and output milestones. Every offset uses a monotonic clock relative to the gateway receiving the request. UTC timestamps remain UTC in the detail view

The gateway returns `x-retail-trace-id`, a server-generated identifier. Native `x-litellm-call-id` and the retail reservation ID remain separate and are associated with that trace. `GET /retail/admin/request-traces/{id}` accepts the trace ID or native call ID and requires a proxy administrator. Ordinary account users cannot retrieve routing internals, diagnostic bodies or cost estimates from this endpoint

## Timing and coverage

Recorded phases can nest or overlap. Their durations must not be added together. Client write time is the sum of ASGI send calls; it measures acceptance by the server's response stack, not delivery to the user's device. Completion of the HTTP response and completion of asynchronous accounting are distinct events. A late settlement updates the same trace without moving the response-complete milestone

HTTP attempts are collected using request and response hooks on gateway-created asynchronous HTTPX clients, including the OpenAI SDK's shared client. They retain the selected deployment, provider, response status and version, bounded response events, and a validated upstream request ID when available in response headers or an error document. This includes failed attempts followed by a retry. User-supplied HTTP clients without these hooks, synchronous requests and other network stacks do not acquire fabricated transport timings

HTTPcore trace events provide connection, request-send and response-header/body boundaries when the underlying transport supplies them. The default aiohttp transport does not expose these HTTPcore events. Missing connection, DNS or TLS measurements remain unrecorded; a connection without a connect event is not automatically labelled reused

The first upstream event, first model output, first reasoning output, first tool arguments and first visible text are separate observations. Initial role frames, start events and heartbeats do not count as model output. The output parser supports Chat Completions, Responses, Anthropic Messages and Gemini. Compressed raw upstream bytes are forwarded intact and are not parsed as plaintext output

Historical logs keep their existing final status, usage and native cost. The new panel marks missing historical trace capture rather than reconstructing stages from total duration. Requests rejected before a native call ID exists can still be retrieved using the server trace ID

## Billing and retention

The panel uses the reservation's frozen price version, discount, expression branch, actual reported usage and ledger entries. It never prices old requests using today's model settings. A stored `provider_cost` from native LiteLLM accounting is labelled a gateway cost estimate, not a supplier invoice or verified purchase cost. Missing cost evidence does not become zero or a claimed profit margin

The gateway captures JSON request bodies before model conversion. Credential keys and recognizable bearer/API tokens are redacted before persistence. Non-JSON and oversized bodies are omitted explicitly. Capture is bounded to 256 KiB and persisted diagnostic JSON to 64 KiB; bounded fields may be shortened in the stored diagnostic copy. This copy is separate from the request forwarded upstream

Body access expires 30 minutes after request receipt. Reads reject expired bodies immediately. A cleanup task removes expired bodies at startup and every 60 seconds, and reads also clear expired bodies. A trace write clears only that trace's expired body, so unrelated expired records do not add row-lock waits to request logging. Late billing callbacks cannot restore a body or extend its expiry. Trace records expire after 30 days; accounting reservations and ledger entries retain their existing lifecycle

No separate OpenTelemetry service is required for this local detail view. Existing OpenTelemetry exports continue independently when configured
