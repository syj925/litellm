"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, CircleAlert, ShieldCheck } from "lucide-react";
import { createParser } from "nuqs";
import type { ReactNode, RefObject } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { LoadingState } from "@/components/shared/LoadingState";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button } from "@/components/ui/button";
import { RetailRequestError, retailRequest, type RetailConfig } from "../api";

export const managementPageSize = 25;
export const managementPageParser = createParser({
  parse: (value) => {
    const number = Number(value);
    return Number.isSafeInteger(number) && number >= 1 && number <= 4001 ? number : null;
  },
  serialize: String,
}).withDefault(1);

export const managementTextParser = createParser({
  parse: (value) => (value.length <= 100 ? value : null),
  serialize: String,
}).withDefault("");

export const managementAccountIdParser = createParser({
  parse: (value) => (value.length <= 191 ? value : null),
  serialize: String,
}).withDefault("");

type ManagementConfig = RetailConfig & {
  organization_management_enabled?: boolean;
  operation_audit_enabled?: boolean;
  report_export_enabled?: boolean;
  object_metadata_enabled?: boolean;
  model_budgets_enabled?: boolean;
};
export type ManagementFeature =
  | "organization_management_enabled"
  | "operation_audit_enabled"
  | "report_export_enabled"
  | "object_metadata_enabled"
  | "model_budgets_enabled";

export function ManagementFeatureGate({
  feature,
  title,
  children,
}: {
  feature: ManagementFeature;
  title: string;
  children: ReactNode;
}) {
  const { accessToken, userId, isAuthorized, isLoading } = useAuthorized();
  const configOptions = {
    queryKey: ["retail", "management-config", userId],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<ManagementConfig>("/config", accessToken, { signal }),
    enabled: isAuthorized && Boolean(accessToken),
    staleTime: 30_000,
    retry: false,
  };
  const config = useQuery(configOptions);
  if (!isLoading && !isAuthorized) return null;
  if (isLoading || config.isPending)
    return <LoadingState description="Loading authorized gateway data." title={`Loading ${title.toLowerCase()}`} />;
  if (config.error) return <ManagementError title={`Unable to load ${title.toLowerCase()}`} error={config.error} />;
  if (config.data?.[feature] !== true) {
    return (
      <StateMessage
        role="status"
        icon={<ShieldCheck className="size-5" />}
        title={`${title} is not enabled`}
        description="This gateway has not enabled this feature. Contact your gateway administrator for access."
      />
    );
  }
  return children;
}

export function ManagementError({ title, error }: { title: string; error: Error }) {
  return (
    <StateMessage
      role="alert"
      tone="destructive"
      icon={<CircleAlert className="size-5" />}
      title={title}
      description={error.message}
    />
  );
}

export function managementConflict(error: Error | null): boolean {
  return error instanceof RetailRequestError && error.status === 409;
}

export function managementQuery(path: string, values: Record<string, string | number | undefined>): string {
  const query = Object.entries(values)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");
  return query ? `${path}?${query}` : path;
}

export function managementTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("en-GB", { timeZone: "Asia/Shanghai", dateStyle: "medium", timeStyle: "short" });
}

export type IdempotencyAttempt = { signature: string; key: string };
export function managementAttempt(ref: RefObject<IdempotencyAttempt | null>, payload: object): string {
  const signature = JSON.stringify(payload);
  if (ref.current?.signature === signature) return ref.current.key;
  const key = crypto.randomUUID();
  ref.current = { signature, key };
  return key;
}

export function ManagementPagination({
  page,
  total,
  loading,
  onChange,
}: {
  page: number;
  total: number;
  loading: boolean;
  onChange: (page: number) => void;
}) {
  const first = total === 0 ? 0 : (page - 1) * managementPageSize + 1;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t px-4 py-3 text-sm text-muted-foreground">
      <span>
        {first.toLocaleString()}–{Math.min(page * managementPageSize, total).toLocaleString()} of{" "}
        {total.toLocaleString()}
      </span>
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          aria-label="Previous page"
          disabled={loading || page <= 1}
          onClick={() => onChange(page - 1)}
        >
          <ChevronLeft />
          Previous
        </Button>
        <span>Page {page}</span>
        <Button
          variant="outline"
          size="sm"
          aria-label="Next page"
          disabled={loading || page * managementPageSize >= total}
          onClick={() => onChange(page + 1)}
        >
          <ChevronRight />
          Next
        </Button>
      </div>
    </div>
  );
}
