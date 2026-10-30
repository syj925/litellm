"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { formatMoney, retailRequest } from "./api";
import {
  expressionTemplates,
  parseUsageBounds,
  parsePreviewParameters,
  type ExpressionPolicy,
  type ExpressionPreview,
} from "./expressionPricing";

const factFields = [
  ["p", "Other uncached input tokens"],
  ["c", "Other output tokens"],
  ["len", "Full prompt context tokens"],
  ["cr", "Other cache read tokens"],
  ["cc", "Other cache write tokens"],
  ["cc5m", "Cache write tokens · 5m"],
  ["cc30m", "Cache write tokens · 30m"],
  ["cc1h", "Cache write tokens · 1h"],
  ["cc12h", "Cache write tokens · 12h"],
  ["img", "Uncached input image tokens"],
  ["img_cr", "Cached image tokens"],
  ["img_o", "Output image tokens"],
  ["ai", "Input audio tokens"],
  ["ao", "Output audio tokens"],
  ["reasoning", "Reasoning tokens"],
] as const;

export default function ExpressionPricingEditor({
  policy,
  onChange,
  accessToken,
  disabled,
}: {
  policy: ExpressionPolicy;
  onChange: (policy: ExpressionPolicy) => void;
  accessToken: string | null;
  disabled: boolean;
}) {
  const [boundsText, setBoundsText] = useState(JSON.stringify(policy.usage_bounds, null, 2));
  const [facts, setFacts] = useState<Record<string, string>>({ p: "1000", c: "500", len: "1000" });
  const [usageText, setUsageText] = useState("{}");
  const [cacheTtlText, setCacheTtlText] = useState("{}");
  const [requestText, setRequestText] = useState("{}");
  const [startedAt, setStartedAt] = useState(() => new Date().toISOString());
  const [serviceTier, setServiceTier] = useState("default");
  const [imageCount, setImageCount] = useState("1");
  const [discount, setDiscount] = useState("1");
  const [localError, setLocalError] = useState<string | null>(null);
  const preview = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      retailRequest<ExpressionPreview>("/admin/pricing-preview", accessToken, {
        method: "POST",
        body: JSON.stringify(body),
      }),
  });
  const change = (next: ExpressionPolicy) => {
    preview.reset();
    onChange(next);
  };
  const changeBounds = (value: string) => {
    const parsed = parseUsageBounds(value);
    setBoundsText(value);
    setLocalError(parsed.error);
    change({ ...policy, usage_bounds: parsed.error ? { "": value } : parsed.bounds });
  };
  const applyTemplate = (template: (typeof expressionTemplates)[number]) => {
    const usage_bounds = template.usage_bounds ?? {};
    setBoundsText(JSON.stringify(usage_bounds, null, 2));
    setUsageText(JSON.stringify(usage_bounds, null, 2));
    setLocalError(null);
    change({ ...policy, expression: template.expression, usage_bounds });
  };
  const simulate = () => {
    const bounds = parseUsageBounds(boundsText);
    const usage = parseUsageBounds(usageText);
    const cacheTtl = parseUsageBounds(cacheTtlText);
    const cacheTtlError =
      cacheTtl.error ||
      (Object.entries(cacheTtl.bounds).some(
        ([ttl, count]) => !/^\d+$/.test(ttl) || Number(ttl) < 1 || !/^\d+$/.test(count),
      )
        ? "Custom cache durations and their token counts must be whole numbers."
        : null);
    const requestParameters = parsePreviewParameters(requestText);
    const invalidFact = Object.values(facts).some((value) => value !== "" && !/^\d+(?:\.\d+)?$/.test(value));
    const invalidInputs = [
      bounds.error,
      usage.error,
      cacheTtlError,
      requestParameters.error,
      invalidFact,
      !/^\d+(?:\.\d+)?$/.test(discount),
      !/^\d+$/.test(imageCount),
    ].some(Boolean);
    if (invalidInputs) {
      setLocalError(
        bounds.error ||
          usage.error ||
          cacheTtlError ||
          requestParameters.error ||
          "Use non-negative decimal usage and discount values, and an integer image count.",
      );
      return;
    }
    if (!/(Z|[+-]\d{2}:\d{2})$/.test(startedAt) || Number.isNaN(Date.parse(startedAt))) {
      setLocalError("Enter an ISO timestamp with Z or an explicit timezone offset.");
      return;
    }
    setLocalError(null);
    const hasTokenUsage =
      Object.entries(facts).some(([key, value]) => key !== "len" && /[1-9]/.test(value)) ||
      Object.values(cacheTtl.bounds).some((value) => /[1-9]/.test(value));
    const body = {
      expression_policy: { ...policy, usage_bounds: bounds.bounds },
      facts: {
        ...Object.fromEntries(factFields.map(([key]) => [key, facts[key] || "0"])),
        ...(hasTokenUsage ? { tokens: "1" } : {}),
        cache_write_ttl_tokens: cacheTtl.bounds,
        usage: usage.bounds,
      },
      request: { ...requestParameters.params, service_tier: serviceTier, n: Number(imageCount) },
      started_at: startedAt,
      discount,
    };
    preview.mutate(body);
  };
  return (
    <section className="space-y-4 rounded-lg border p-4" aria-labelledby="expression-editor-title">
      <h3 id="expression-editor-title" className="font-medium">
        Dynamic pricing rule
      </h3>
      <p className="text-xs leading-6 text-muted-foreground">
        Token coefficients are USD per 1M tokens. The server divides the expression result by 1,000,000. Use fixed(USD)
        for per-request or per-unit fees. Customer discounts apply to the resulting amount.
      </p>
      <Field>
        <FieldLabel htmlFor="price-expression">Pricing expression</FieldLabel>
        <Textarea
          id="price-expression"
          className="min-h-36 font-mono text-xs"
          value={policy.expression}
          disabled={disabled}
          required
          maxLength={16000}
          onChange={(event) => change({ ...policy, expression: event.target.value })}
        />
      </Field>
      <Field>
        <FieldLabel htmlFor="price-timezone">Rule timezone</FieldLabel>
        <Input
          id="price-timezone"
          value={policy.timezone}
          disabled={disabled}
          required
          placeholder="Asia/Shanghai"
          onChange={(event) => change({ ...policy, timezone: event.target.value })}
        />
        <p className="text-xs text-muted-foreground">
          IANA timezone used by hour(), minute(), weekday(), month() and day(). The server validates it.
        </p>
      </Field>
      <Field>
        <FieldLabel htmlFor="price-usage-bounds">Reservation bounds for u() usage (JSON)</FieldLabel>
        <Textarea
          id="price-usage-bounds"
          className="font-mono text-xs"
          value={boundsText}
          disabled={disabled}
          onChange={(event) => changeBounds(event.target.value)}
        />
        <p className="text-xs leading-5 text-muted-foreground">
          Declare the maximum for each u(&apos;name&apos;) referenced by the rule, for example {`{"images":"4"}`}. These
          bounds reserve funds; usage comes from reported usage, not an estimated request count.
        </p>
      </Field>
      <details className="rounded-md bg-muted/40 p-3 text-xs leading-6">
        <summary className="cursor-pointer font-medium">Variables, conditions and units</summary>
        <dl className="mt-3 grid gap-2 sm:grid-cols-2">
          {factFields.map(([key, label]) => (
            <div key={key}>
              <dt className="inline font-mono font-semibold">{key}</dt>
              <dd className="inline">: {label}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-3">
          Arithmetic: + - * /. Conditions: &gt; &gt;= &lt; &lt;= == !=, &amp;&amp;, ||, !, condition ? yes : no.
          Functions: min, max, abs, ceil, floor. tier(&apos;name&apos;, amount) records the selected branch.
          param(&apos;service_tier&apos;) reads an allowed request parameter. weekday() is 0 Sunday to 6 Saturday. Time
          helpers accept an optional IANA timezone. cache_write(&apos;750&apos;) reads write tokens for any duration in
          seconds. Use either its named alias or cache_write() for a duration, never both. reported(&apos;tokens&apos;)
          checks for usable reported token usage before choosing a per-image fallback.
        </p>
        <p>
          When an image, audio or reasoning variable is referenced, billing excludes that subset from p or c so it is
          charged separately. Referenced cached image tokens are excluded from cr; referenced write durations are
          excluded from cc. Synthetic values here must already exclude those separately priced subsets.
          u(&apos;name&apos;) reads a reported numeric usage unit.
        </p>
      </details>
      <div className="space-y-2">
        <p className="text-xs font-medium">Example templates. Synthetic prices for editing, not vendor quotations.</p>
        <div className="flex flex-wrap gap-2">
          {expressionTemplates.map((template) => (
            <Button
              key={template.id}
              type="button"
              size="xs"
              variant="outline"
              disabled={disabled}
              onClick={() => applyTemplate(template)}
            >
              {template.label}
            </Button>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          A template replaces this local draft. Save base price applies it to future requests.
        </p>
      </div>
      <details className="rounded-lg border p-3" open>
        <summary className="cursor-pointer text-sm font-medium">Test rule with synthetic usage</summary>
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
          {factFields.map(([key, label]) => (
            <Field key={key}>
              <FieldLabel htmlFor={`preview-${key}`} className="text-xs">
                {label} ({key})
              </FieldLabel>
              <Input
                id={`preview-${key}`}
                inputMode="decimal"
                value={facts[key] ?? "0"}
                disabled={disabled || preview.isPending}
                onChange={(event) => {
                  preview.reset();
                  setFacts({ ...facts, [key]: event.target.value });
                }}
              />
            </Field>
          ))}
          <Field>
            <FieldLabel htmlFor="preview-service-tier">Service tier</FieldLabel>
            <Input
              id="preview-service-tier"
              value={serviceTier}
              onChange={(event) => {
                preview.reset();
                setServiceTier(event.target.value);
              }}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="preview-n">Requested image count (n)</FieldLabel>
            <Input
              id="preview-n"
              inputMode="numeric"
              value={imageCount}
              onChange={(event) => {
                preview.reset();
                setImageCount(event.target.value);
              }}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="preview-discount">Discount factor</FieldLabel>
            <Input
              id="preview-discount"
              inputMode="decimal"
              value={discount}
              onChange={(event) => {
                preview.reset();
                setDiscount(event.target.value);
              }}
            />
          </Field>
        </div>
        <div className="mt-3 space-y-3">
          <Field>
            <FieldLabel htmlFor="preview-started-at">Request start (ISO timestamp with offset)</FieldLabel>
            <Input
              id="preview-started-at"
              value={startedAt}
              onChange={(event) => {
                preview.reset();
                setStartedAt(event.target.value);
              }}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="preview-request">Additional synthetic request parameters (JSON)</FieldLabel>
            <Textarea
              id="preview-request"
              className="font-mono text-xs"
              value={requestText}
              onChange={(event) => {
                preview.reset();
                setRequestText(event.target.value);
              }}
            />
            <p className="text-xs text-muted-foreground">
              Optional quality, size, resolution, duration, reasoning_effort and prompt_cache_retention values for
              param().
            </p>
          </Field>
          <Field>
            <FieldLabel htmlFor="preview-cache-ttl">
              Custom cache write token counts by duration seconds (JSON)
            </FieldLabel>
            <Textarea
              id="preview-cache-ttl"
              className="font-mono text-xs"
              value={cacheTtlText}
              onChange={(event) => {
                preview.reset();
                setCacheTtlText(event.target.value);
              }}
            />
            <p className="text-xs text-muted-foreground">
              For example {`{"750":"100"}`}. These counts are excluded from cc when cache_write() references their
              duration.
            </p>
          </Field>
          <Field>
            <FieldLabel htmlFor="preview-usage">Reported u() usage (JSON)</FieldLabel>
            <Textarea
              id="preview-usage"
              className="font-mono text-xs"
              value={usageText}
              onChange={(event) => {
                preview.reset();
                setUsageText(event.target.value);
              }}
            />
          </Field>
          <Button type="button" variant="secondary" disabled={disabled || preview.isPending} onClick={simulate}>
            {preview.isPending ? "Calculating..." : "Simulate charge"}
          </Button>
          <p className="text-xs text-muted-foreground">
            This read-only simulation uses the billing compiler. It does not save prices, reserve funds or charge a
            wallet.
          </p>
        </div>
      </details>
      {(localError || preview.error) && (
        <Alert variant="error">
          <AlertTitle>Check dynamic pricing</AlertTitle>
          <AlertDescription>{localError || preview.error?.message}</AlertDescription>
        </Alert>
      )}
      {preview.data && (
        <section aria-label="Simulated charge" className="space-y-3 rounded-lg bg-muted/40 p-4 text-sm">
          <p className="font-semibold">Simulated charge: {formatMoney(preview.data.amount)} USD</p>
          <p>
            Before discount: {formatMoney(preview.data.undiscounted_amount)} · Discount factor: {preview.data.discount}
          </p>
          <p>Selected branches: {preview.data.tiers.join(", ") || "No named branch"}</p>
          <p className="break-all">
            Request start: {preview.data.started_at} · Timezone: {preview.data.timezone}
          </p>
          <details>
            <summary className="cursor-pointer">Evaluated variables and usage</summary>
            <pre className="mt-2 overflow-auto whitespace-pre-wrap text-xs">
              {JSON.stringify({ variables: preview.data.variables, usage: preview.data.usage ?? {} }, null, 2)}
            </pre>
          </details>
        </section>
      )}
    </section>
  );
}
