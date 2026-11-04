"use client";

import { useQuery } from "@tanstack/react-query";
import { useQueryStates } from "nuqs";
import { useCallback, useEffect, useId, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RetailRequestError, retailRequest } from "../../api";
import { ManagementPagination, managementPageSize, managementQuery } from "../../management/ManagementShared";
import { consumptionWindow } from "../financeInput";
import { DelegatedError, useDelegationConfiguration, useFinanceScopes } from "./DelegatedShared";
import DelegatedResults from "./DelegatedResults";
import DelegatedWindow from "./DelegatedWindow";
import DelegatedReports from "./DelegatedReports";
import { delegatedAccessLost, delegatedActor, delegatedParsers, verifiedFinanceAccess } from "./input";
import type { DelegatedConsumption, FinanceAccess } from "./types";

function ScopePicker({ onChoose, onUnavailable }: { onChoose: (id: string) => void; onUnavailable: () => void }) {
  const [filters, setFilters] = useQueryStates(delegatedParsers);
  const scopes = useFinanceScopes(true, filters.scope_q, filters.scope_page);
  const id = useId();
  useEffect(() => {
    if (delegatedAccessLost(scopes.error)) onUnavailable();
  }, [scopes.error, onUnavailable]);
  const visible = !scopes.error && scopes.isFetchedAfterMount ? scopes.data : undefined;
  return (
    <section className="space-y-3" aria-label="Authorized finance organizations">
      <div className="space-y-2">
        <Label htmlFor={`${id}-search`}>Search authorized organizations</Label>
        <Input
          id={`${id}-search`}
          value={filters.scope_q}
          maxLength={100}
          onChange={(event) => void setFilters({ scope_q: event.target.value, scope_page: 1 })}
        />
      </div>
      {scopes.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading authorized organizations…
        </p>
      )}
      {scopes.error && <DelegatedError error={scopes.error} />}
      {visible && (
        <>
          <div className="space-y-2">
            <Label htmlFor={`${id}-organization`}>Organization</Label>
            <select
              id={`${id}-organization`}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={filters.organization}
              disabled={scopes.isFetching}
              onChange={(event) => onChoose(event.target.value)}
            >
              <option value="">Choose an authorized organization</option>
              {filters.organization && !visible.items.some((item) => item.id === filters.organization) && (
                <option value={filters.organization}>Selected organization</option>
              )}
              {visible.items
                .filter((item) => item.can_view)
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
            </select>
          </div>
          <ManagementPagination
            page={filters.scope_page}
            total={visible.total}
            loading={scopes.isFetching}
            onChange={(scope_page) => void setFilters({ scope_page })}
          />
          {visible.total === 0 && (
            <p role="status" className="text-sm text-muted-foreground">
              No finance access has been granted to your account.
            </p>
          )}
        </>
      )}
    </section>
  );
}

function FinanceUsage({
  access,
  accessToken,
  actorId,
  onAccessLost,
  onRefreshAccess,
  exportsEnabled,
}: {
  access: FinanceAccess & { access_revision: string };
  accessToken: string;
  actorId: string | null;
  onAccessLost: () => void;
  onRefreshAccess: () => void;
  exportsEnabled: boolean;
}) {
  const [filters, setFilters] = useQueryStates(delegatedParsers);
  const [instance] = useState(() => crypto.randomUUID());
  const [defaultWindow] = useState(() => {
    const end = new Date(Math.floor(Date.now() / 60000) * 60000);
    return { start: new Date(end.getTime() - 7 * 86400000), end };
  });
  const start = filters.start ?? defaultWindow.start;
  const end = filters.end ?? defaultWindow.end;
  const error = consumptionWindow(filters.start, filters.end);
  const parameters = {
    start: start.toISOString(),
    end: end.toISOString(),
    limit: managementPageSize,
    offset: (filters.page - 1) * managementPageSize,
  };
  const options = {
    queryKey: [
      "retail",
      "delegated-finance",
      actorId,
      access.organization_id,
      access.access_revision,
      instance,
      "usage",
      parameters,
    ],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      const result = await retailRequest<DelegatedConsumption>(
        managementQuery(`/organizations/${encodeURIComponent(access.organization_id)}/finance/consumption`, parameters),
        accessToken,
        { signal },
      );
      if (result.organization_id !== access.organization_id || result.access_revision !== access.access_revision)
        throw new RetailRequestError("Finance authorization changed", 403);
      return result;
    },
    enabled: !error,
    retry: false,
  };
  const usage = useQuery(options);
  useEffect(() => {
    if (delegatedAccessLost(usage.error)) onAccessLost();
  }, [usage.error, onAccessLost]);
  const visible = !usage.error && !error ? usage.data : undefined;
  return (
    <div className="space-y-5">
      <DelegatedWindow
        key={`${start.toISOString()}:${end.toISOString()}`}
        start={start}
        end={end}
        disabled={usage.isFetching}
        onApply={(start, end) => void setFilters({ start, end, page: 1 })}
      />
      <p className="text-xs text-muted-foreground">
        Up to 31 days and 10,000 accepted requests. Existing attributed history is included.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {usage.isPending && !error && <p role="status">Loading organization finance…</p>}
      {usage.error && <DelegatedError error={usage.error} />}
      <DelegatedResults
        data={visible}
        page={filters.page}
        loading={usage.isFetching}
        onPage={(page) => void setFilters({ page })}
        exports={
          access.can_export &&
          exportsEnabled && (
            <DelegatedReports
              access={access}
              preview={visible}
              accessToken={accessToken}
              actorId={actorId}
              instance={instance}
              onAccessRefresh={onRefreshAccess}
            />
          )
        }
      />
    </div>
  );
}

function FinanceWorkspace({
  accessToken,
  actorId,
  exportsEnabled,
}: {
  accessToken: string;
  actorId: string | null;
  exportsEnabled: boolean;
}) {
  const [filters, setFilters] = useQueryStates(delegatedParsers);
  const [check, setCheck] = useState(0);
  const [blocked, setBlocked] = useState(false);
  const options = {
    queryKey: ["retail", "delegated-finance-access", actorId, filters.organization, check],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<FinanceAccess>(
        `/organizations/${encodeURIComponent(filters.organization)}/finance/access`,
        accessToken,
        { signal },
      ),
    enabled: Boolean(filters.organization),
    retry: false,
    staleTime: 0,
    refetchOnMount: "always" as const,
    refetchInterval: 15000,
  };
  const access = useQuery(options);
  const refetchAccess = access.refetch;
  const unavailable = useCallback(() => setBlocked(true), []);
  const refreshAccess = useCallback(() => {
    void refetchAccess();
  }, [refetchAccess]);
  const current = access.isFetchedAfterMount && !access.error && !blocked ? access.data : undefined;
  const reload = () => {
    setBlocked(false);
    setCheck((value) => value + 1);
  };
  const choose = (organization: string) => {
    setBlocked(false);
    void setFilters({ organization: organization || null, page: 1, export_page: 1 });
  };
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>Organization finance</PageHeaderTitle>
          <PageHeaderDescription>
            Read usage and finalized retail charges within your explicit organization grant.
          </PageHeaderDescription>
        </PageHeader>
        <Button variant="outline" onClick={reload} disabled={access.isFetching}>
          Reload access
        </Button>
      </div>
      <ScopePicker onChoose={choose} onUnavailable={unavailable} />
      {filters.organization && access.isPending && <p role="status">Verifying finance access…</p>}
      {access.error && <DelegatedError error={access.error} />}
      {blocked && (
        <p role="alert" className="text-sm text-destructive">
          Finance access changed. Reload access before viewing more data.
        </p>
      )}
      {current && !verifiedFinanceAccess(current, filters.organization) && (
        <p role="status" className="text-sm text-muted-foreground">
          Your account has no finance view grant for this organization.
        </p>
      )}
      {verifiedFinanceAccess(current, filters.organization) && (
        <FinanceUsage
          key={`${filters.organization}:${current.access_revision}:${check}`}
          access={current}
          accessToken={accessToken}
          actorId={actorId}
          onAccessLost={unavailable}
          onRefreshAccess={refreshAccess}
          exportsEnabled={exportsEnabled}
        />
      )}
    </div>
  );
}

export default function DelegatedFinancePanel() {
  const { accessToken, userId, userRole, isViewOnly, isAuthorized } = useAuthorized();
  const { enabled, config } = useDelegationConfiguration();
  if (!isAuthorized || !accessToken || !delegatedActor(userRole, isViewOnly)) return null;
  if (config.isPending)
    return (
      <Page>
        <p role="status">Loading finance availability…</p>
      </Page>
    );
  if (!enabled)
    return (
      <Page>
        <p role="status">Organization finance delegation is not enabled.</p>
      </Page>
    );
  return (
    <Page>
      <FinanceWorkspace
        accessToken={accessToken}
        actorId={userId}
        exportsEnabled={config.data?.report_export_enabled === true}
      />
    </Page>
  );
}
