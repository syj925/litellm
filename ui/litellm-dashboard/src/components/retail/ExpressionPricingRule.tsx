import type { ExpressionPolicy } from "./expressionPricing";

export default function ExpressionPricingRule({ policy }: { policy: ExpressionPolicy }) {
  return (
    <section aria-label="Dynamic pricing rule" className="space-y-3 p-4 text-sm">
      <p className="font-medium">Dynamic pricing</p>
      <p className="text-xs leading-6 text-muted-foreground">
        The charge depends on the usage and conditions in this rule. Token coefficients are USD per 1M tokens; fixed()
        amounts are USD. Your applicable discount applies to the calculated charge.
      </p>
      <pre className="overflow-auto rounded-md bg-muted/40 p-3 font-mono text-xs leading-6 whitespace-pre-wrap break-words">
        {policy.expression}
      </pre>
      <p className="text-xs text-muted-foreground">
        Timezone: {policy.timezone} · The request start time selects time conditions. weekday() uses Sunday = 0 and
        Saturday = 6.
      </p>
      <details className="text-xs leading-6 text-muted-foreground">
        <summary className="cursor-pointer">Rule variables</summary>
        <p>
          p: other uncached input; c: other output; len: full prompt context; cr: other cache reads; cc: other cache
          writes; cc5m / cc30m / cc1h / cc12h: cache write durations. img / img_cr / img_o: image input, cached input
          and output tokens. ai / ao: audio input and output tokens. reasoning: reasoning output tokens. Referenced
          modality subsets are separated from p or c. Referenced cached image tokens are separated from cr; referenced
          write durations are separated from cc. u(&apos;name&apos;) is a reported usage unit. tier() names the applied
          branch; param() reads an allowed request parameter. cache_write(&apos;750&apos;) reads separately priced write
          tokens for a custom duration in seconds. reported(&apos;tokens&apos;) selects token pricing when usable token
          usage is present.
        </p>
      </details>
    </section>
  );
}
