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
