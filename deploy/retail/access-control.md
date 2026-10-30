# Retail portal access boundaries

The retail portal uses LiteLLM roles, account resource grants and per-key policies together. A menu item is never an authorization grant. The dashboard route boundary runs before protected page content mounts; server authorization and response projections also apply to direct API requests.

| Capability | Customer | Team / organization administrator | Proxy administrator |
| --- | --- | --- | --- |
| Home, personal usage and request logs | Own data | Own / delegated scope | Global |
| Models | Read-only account-authorized catalog | Authorized catalog | Deployment management |
| Keys | Own AI API keys | Native delegated team scope | Global key management |
| Wallet and Billing | Own balance and actual charges | Own account | Customer wallets and pricing administration |
| Teams / organizations | No administrative page | Explicit delegated scope | Global |
| Channels, resource pools, credentials and routing configuration | Denied | Denied | Allowed |
| Channel activity, performance and upstream error analytics | Denied | Denied | Writable proxy administrators only |
| Agent / vector-store administration | Hidden | Only when explicitly enabled for team admins | Allowed |
| MCP, guardrail, policy, tool and response-cache administration | Hidden | Hidden unless independently authorized by server policy | Allowed |
| Plugins, worker controls and request transformation debugging | Denied | Denied | Writable proxy admins only |

Proxy administrator viewers retain supported native read-only administrative views, but cannot create keys, invoke models or access the retail administration pages that require writable proxy-admin authorization. Internal-user viewers cannot mutate keys or other management resources. Native inference permissions for internal-user viewers remain subject to account grants and wallet enforcement.

## Public model discovery and personal models

The top navigation's Models link opens `/ui/models/`, a standalone catalog available without signing in. The sidebar's Models link opens `/ui/model-catalog/` inside the authenticated dashboard and shows only the account's authorized pool/model pairs

`GET /retail/public/model-catalog` describes all configured non-wildcard public request model names, deduplicated across deployments. Model information remains discoverable when a resource pool is disabled or restricted. Public resource offers include only enabled, public pools; private or disabled pool identities and all customer discounts remain omitted. A model can have public reference specifications and prices without publicly listed resources. This is discovery information and does not grant invocation access

`GET /retail/model-catalog` requires authentication and returns only authorized pool/model pairs with the current account's applicable prices. Reference rates use the existing published pricing basis. Customer quotes use the same discount precedence and decimal arithmetic as wallet billing. The public endpoint never reads personal wallet or discount state

Both views reuse branded model identities, query-backed filters, list/table views, comparison and detail panels. Request model IDs remain exact. Unknown specifications stay unknown, and the UI derives factual summaries only from available declared metadata. Performance and benchmark claims require measured data and are not synthesized

## Customer key rules

- Personal keys default to AI API routes and an explicit `auto` resource policy.
- Customers cannot request Management / Full Access presets, custom route lists, a different owner, the dashboard-session team ID, arbitrary metadata, model aliases, per-key provider configuration or router overrides.
- Existing personal keys with an empty route list are treated as AI-only. Server-issued dashboard sessions retain the scoped operations required by the UI.
- Account access and per-key pool/model restrictions intersect. A zero balance cannot invoke paid models. Fixed-pool routing cannot silently fall back to another pool.
- Real team keys retain native team-membership and delegated-role checks; a client-supplied team ID does not confer membership.

## Response and billing boundaries

Customer model discovery uses authorized public models and deployments. Supplier addresses, credentials, internal routing parameters, fallback configuration and provider costs are not customer model information. Customer key and log responses likewise omit administrative configuration and supplier details.

LiteLLM native `spend` counters track gateway/provider accounting and are not the retail wallet's discounted customer ledger. Customer pages link to Wallet / Billing for actual charges rather than presenting those native counters as invoices. Budget caps remain available as invocation controls.

## Regression coverage

Targeted tests cover dashboard navigation, search, direct routes, key create/edit payloads, ownership, viewer roles, model projections, pool grants and safe responses. Runtime verification uses a separate database and local mock upstream: customer creation, forbidden payload fields, cross-account access, zero-balance refusal, funded pool routing, rotation and administrator compatibility. No real supplier requests or production wallet credits are needed for these checks.

Upstream role reference: https://docs.litellm.ai/docs/proxy/access_control
