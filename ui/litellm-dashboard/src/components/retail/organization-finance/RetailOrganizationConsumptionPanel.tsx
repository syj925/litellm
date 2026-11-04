"use client";

import { useQuery } from "@tanstack/react-query";
import { Building2, RefreshCw } from "lucide-react";
import { useQueryStates } from "nuqs";
import { useCallback, useId, useState, type FormEvent } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { retailRequest } from "../api";
import { ManagementFeatureGate, managementPageSize, managementQuery } from "../management/ManagementShared";
import { beijingTimeInput, managementInterval } from "../management/time";
import ConsumptionResults from "./ConsumptionResults";
import { consumptionParsers, consumptionWindow, financeAccessLost } from "./financeInput";
import { FinanceError } from "./FinanceShared";
import OrganizationPicker from "./OrganizationPicker";
import type { OrganizationChoice, OrganizationConsumption } from "./types";

function selectedOrganization(id: string, report: OrganizationConsumption | undefined): OrganizationChoice | undefined {
  if (!id) return undefined;
  return { id, name: report?.organization_name ?? "Selected organization", enabled: report?.organization_enabled };
}

function WindowForm({
  start,
  end,
  disabled,
  onApply,
}: {
  start: Date;
  end: Date;
  disabled: boolean;
  onApply: (start: Date, end: Date) => void;
}) {
  const id = useId();
  const [from, setFrom] = useState(beijingTimeInput(start.toISOString()));
  const [until, setUntil] = useState(beijingTimeInput(end.toISOString()));
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const result = managementInterval(from, until, true);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    if (result.interval) {
      setError(null);
      onApply(new Date(result.interval.start_at), new Date(result.interval.end_at));
    }
  };
  return (
    <form className="space-y-3" onSubmit={submit}>
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-2">
          <Label htmlFor={`${id}-start`}>Accepted from (Beijing)</Label>
          <Input
            id={`${id}-start`}
            type="datetime-local"
            value={from}
            disabled={disabled}
            onChange={(event) => setFrom(event.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${id}-end`}>Accepted before (Beijing)</Label>
          <Input
            id={`${id}-end`}
            type="datetime-local"
            value={until}
            disabled={disabled}
            onChange={(event) => setUntil(event.target.value)}
          />
        </div>
        <Button type="submit" variant="outline" disabled={disabled}>
          Apply window
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}

function ConsumptionWorkspace({ accessToken, actorId }: { accessToken: string; actorId: string | null }) {
  const [filters, setFilters] = useQueryStates(consumptionParsers);
  const [defaultWindow] = useState(() => {
    const end = new Date(Math.floor(Date.now() / 60_000) * 60_000);
    return { start: new Date(end.getTime() - 7 * 24 * 60 * 60 * 1000), end };
  });
  const [authorityLost, setAuthorityLost] = useState(false);
  const [pickerVersion, setPickerVersion] = useState(0);
  const unavailable = useCallback(() => setAuthorityLost(true), []);
  const windowError = consumptionWindow(filters.start, filters.end);
  const start = filters.start ?? defaultWindow.start;
  const end = filters.end ?? defaultWindow.end;
  const parameters = {
    start: start.toISOString(),
    end: end.toISOString(),
    limit: managementPageSize,
    offset: (filters.page - 1) * managementPageSize,
  };
  const reportOptions = {
    queryKey: [
      "retail",
      "organization-finance",
      actorId,
      "consumption",
      filters.organization,
      start.toISOString(),
      end.toISOString(),
      filters.page,
    ],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<OrganizationConsumption>(
        managementQuery(`/admin/organizations/${encodeURIComponent(filters.organization)}/consumption`, parameters),
        accessToken,
        { signal },
      ),
    enabled: Boolean(filters.organization) && !windowError && !authorityLost,
    retry: false,
  };
  const report = useQuery(reportOptions);
  const visible = !report.error && !authorityLost && !windowError ? report.data : undefined;
  const lost = authorityLost || financeAccessLost(report.error);
  const loadingReport = filters.organization && report.isPending && !windowError;
  const refresh = async () => {
    setPickerVersion((version) => version + 1);
    const result = filters.organization && !windowError ? await report.refetch() : null;
    if (!result?.error) setAuthorityLost(false);
  };
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Building2 />
            Organization consumption
          </PageHeaderTitle>
          <PageHeaderDescription>
            Requests accepted in this window, with final retail charges as they settle.
          </PageHeaderDescription>
        </PageHeader>
        <Button variant="outline" onClick={() => void refresh()} disabled={report.isFetching}>
          <RefreshCw />
          Reload preview
        </Button>
      </div>
      {lost ? (
        <p role="alert" className="text-sm text-destructive">
          Organization finance access changed. Reload to check your current access.
        </p>
      ) : (
        <>
          <div className="grid gap-5 lg:grid-cols-2">
            <OrganizationPicker
              key={pickerVersion}
              refreshKey={pickerVersion}
              accessToken={accessToken}
              actorId={actorId}
              value={filters.organization}
              current={selectedOrganization(filters.organization, visible)}
              onSelect={(organization) => void setFilters({ organization: organization || null, page: 1 })}
              onUnavailable={unavailable}
            />
            <div className="space-y-3">
              <WindowForm
                key={`${start.toISOString()}:${end.toISOString()}`}
                start={start}
                end={end}
                disabled={report.isFetching}
                onApply={(nextStart, nextEnd) => void setFilters({ start: nextStart, end: nextEnd, page: 1 })}
              />
              <p className="max-w-prose text-xs text-muted-foreground">
                Up to 31 days and 10,000 accepted requests. Narrow the window if it exceeds this limit.
              </p>
            </div>
          </div>
          {windowError && (
            <p role="alert" className="text-sm text-destructive">
              {windowError}
            </p>
          )}
          {!filters.organization && (
            <p role="status" className="text-sm text-muted-foreground">
              Choose an organization to preview its consumption.
            </p>
          )}
          {loadingReport && <p role="status">Loading organization consumption…</p>}
          {report.error && <FinanceError error={report.error} context="consumption" />}
          {visible && (
            <ConsumptionResults
              report={visible}
              page={filters.page}
              loading={report.isFetching}
              onPage={(page) => void setFilters({ page })}
            />
          )}
        </>
      )}
    </div>
  );
}

export default function RetailOrganizationConsumptionPanel() {
  const { accessToken, userId, userRole, isViewOnly, isAuthorized } = useAuthorized();
  if (!isAuthorized || !accessToken || !canUseAdminControlPlane({ userRole, isViewOnly })) return null;
  return (
    <Page>
      <ManagementFeatureGate feature="org_finance_enabled" title="Organization finance">
        <ConsumptionWorkspace accessToken={accessToken} actorId={userId} />
      </ManagementFeatureGate>
    </Page>
  );
}
