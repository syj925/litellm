# Catalog pricing sources

Verified on 2026-11-02. These rules describe retail reference prices. Loading the catalog does not save or activate a model price, change an existing saved price, or establish the supplier's final invoice

## Gemini context and calendar rules

The [Gemini Developer API pricing page](https://ai.google.dev/gemini-api/docs/pricing) lists Gemini 2.5 Pro input, output and cache reads above and below 200K input tokens for Standard, Batch and Flex. Missing Batch/Flex long-context fields are derived from the published long-context rate and the existing short-context service ratio. Explicit source fields are preserved

The same page schedules Gemini 4 Argon input/output/cache-read rates to change from USD 2/10/0.1 per million tokens to 4/20/0.2 on December 1, 2026. The calendar rule applies only to that exact model and provider family with the verified starting rates, uses the frozen request date in UTC, and multiplies token charges before tool fees. A source already carrying the later rates is not multiplied again. Conflicting rates require review

The [Google Cloud pricing page](https://cloud.google.com/model-garden/pricing) separately lists global and non-global Argon prices and the same date boundary. Its introductory offer is implemented through promotional credits on net spend; this retail calendar does not reproduce supplier credit accounting

## Grounding and free allowances

[Google's grounding documentation](https://ai.google.dev/gemini-api/docs/google-search) distinguishes per-query billing on Gemini 3 and newer from per-grounded-prompt billing on older models. Queries must be actually executed; tool declarations, URLs and citations do not prove a chargeable count

Developer API and Google Cloud free allowances differ. In particular, the source catalog's generic Gemini 2.5 Pro allowance does not safely represent both Search and Maps. Shared provider allowances are not passed through by the retail reference rule. Reported grounding usage uses the listed paid rate, and the price page exposes that policy. Allocating supplier allowances would require provider account/project identity, separate tools and shared groups, atomic periodic counters and a verified reset boundary

## Claude modifiers

[Claude data residency](https://platform.claude.com/docs/en/manage-claude/data-residency) specifies a US-only multiplier on token categories and documents workspace defaults and the actual response geography. [Fast mode](https://platform.claude.com/docs/en/build-with-claude/fast-mode) has its own multiplier and reports actual speed, including an older-model exception that can run at standard speed despite a fast request. Settlement uses authoritative response attributes and trusted deployment location; it does not infer them from a model's name

The [Claude pricing page](https://platform.claude.com/docs/en/about-claude/pricing) documents cache writes, model-specific cache read rates, the Haiku 5.5 full-input context threshold, and separate per-search fees. The generated modifier multiplies token charges before adding search charges

## OpenAI routing and service tiers

[Fast mode](https://developers.openai.com/api/docs/guides/fast-mode) documents the Fast/Priority aliases and actual-response tier, including fallback to standard pricing. Regional multipliers apply to token charges. Official EU/US hosts identify the deployment region through the existing endpoint resolver. Compatible gateways use global retail list rates unless the server deployment explicitly sets `model_info.pricing_data_residency`; this does not establish where the supplier actually processed a request. Customer request fields cannot override that setting

[OpenAI pricing](https://developers.openai.com/api/docs/pricing) has endpoint-specific Decisions coefficients. They do not change Chat or Responses rates. Decisions, asynchronous Batch and Realtime remain outside the supported prepaid lifecycle; sending `service_tier: batches` on a synchronous request cannot select an asynchronous discounted price. Unknown endpoint-specific cost fields remain subject to review

[Image generation pricing](https://developers.openai.com/api/docs/guides/image-generation) separates text input, image input and image output tokens. Published per-image output estimates cannot substitute for unreported total usage. Unknown image/cache quantities remain pending. Tool-specific search-preview/content-token exceptions outside these verified catalog entries are not inferred from one generic per-search field

## Verification limits

The catalog lacks some published service/context combinations and future schedules. The supplements above cover only exact verified models. `gemini-3-flash` and `gemini-3.1-pro` have no verified stable alias mapping in this review and remain subject to manual pricing review. Preview suffixes are not inferred or silently substituted

Prices can change after this verification date. Saved requests retain their frozen policy and version. Cached-token modality details, actual service tiers, search counts and endpoint location must be available to settle rules that depend on them; a published price is not evidence that every upstream response reports those facts
