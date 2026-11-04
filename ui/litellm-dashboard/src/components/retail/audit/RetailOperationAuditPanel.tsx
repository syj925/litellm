"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { ClipboardList, RefreshCw, Search } from "lucide-react";
import { useQueryStates } from "nuqs";
import { useId, useState, type FormEvent } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import CopyButton from "@/components/shared/CopyButton";
import { Alert, AlertDescription } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { isProxyAdminRole } from "@/utils/roles";
import { retailRequest, type PageResult } from "../api";
import {
  ManagementError,
  ManagementFeatureGate,
  ManagementPagination,
  managementAccountIdParser,
  managementPageParser,
  managementPageSize,
  managementQuery,
  managementTextParser,
  managementTimestamp,
} from "../management/ManagementShared";
import { beijingTimeInput, managementInterval } from "../management/time";
import type { RetailOrganization } from "../organizations/types";
import AuditOrganizationSearch from "./AuditOrganizationSearch";

interface OperationEvent {
  id: string;
  organization_id: string | null;
  actor_id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  result: "succeeded";
  version_before: number | null;
  version_after: number | null;
  created_at: string;
  changes: Record<string, unknown>;
}

const auditParsers = {
  organization: managementTextParser,
  action: managementTextParser,
  actor: managementAccountIdParser,
  start: managementTextParser,
  end: managementTextParser,
  page: managementPageParser,
  scope_query: managementTextParser,
  scope_page: managementPageParser,
};
type AppliedAuditFilters = { organization: string; action: string; actor: string; start: string; end: string };
type AuditFilters = AppliedAuditFilters & { page: number; scope_query: string; scope_page: number };
const inputClass = "h-9 w-full rounded-md border border-input bg-background px-3 text-sm";
const eventLabels: Record<string, string> = {
  "organization.created": "Organization created",
  "organization.updated": "Organization updated",
  "organization.member_associated": "Account associated",
  "organization.member_updated": "Membership updated",
  "report.created": "Report requested",
  "report.completed": "Report completed",
  "report.failed": "Report failed",
  "object_metadata.updated": "Business labels updated",
  "model_budget.created": "Model budget created",
  "model_budget.updated": "Model budget updated",
};

function AuditFiltersForm({
  filters,
  admin,
  organizations,
  onApply,
}: {
  filters: AuditFilters;
  admin: boolean;
  organizations: RetailOrganization[];
  onApply: (filters: AppliedAuditFilters) => void;
}) {
  const formId = useId();
  const [organization, setOrganization] = useState(filters.organization);
  const [action, setAction] = useState(filters.action);
  const [actor, setActor] = useState(filters.actor);
  const [start, setStart] = useState(beijingTimeInput(filters.start));
  const [end, setEnd] = useState(beijingTimeInput(filters.end));
  const [error, setError] = useState("");
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const interval = managementInterval(start, end);
    if (!interval.ok) {
      setError(interval.error);
      return;
    }
    setError("");
    const appliedFilters = {
      organization: organization.trim(),
      action,
      actor: admin ? actor.trim() : "",
      start: interval.interval?.start_at ?? "",
      end: interval.interval?.end_at ?? "",
    };
    onApply(appliedFilters);
  };
  return (
    <form onSubmit={submit} className="space-y-4 rounded-xl border p-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <div className="space-y-2">
          <Label htmlFor={`${formId}-organization`}>Organization</Label>
          {admin ? (
            <Input
              id={`${formId}-organization`}
              value={organization}
              onChange={(event) => setOrganization(event.target.value)}
              maxLength={100}
              placeholder="All organizations, or exact ID"
            />
          ) : (
            <select
              id={`${formId}-organization`}
              className={inputClass}
              value={organization}
              onChange={(event) => setOrganization(event.target.value)}
            >
              <option value="">Select your organization</option>
              {organizations.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-action`}>Action</Label>
          <select
            id={`${formId}-action`}
            className={inputClass}
            value={action}
            onChange={(event) => setAction(event.target.value)}
          >
            <option value="">All actions</option>
            {Object.entries(eventLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        {admin && (
          <div className="space-y-2">
            <Label htmlFor={`${formId}-actor`}>Actor account ID</Label>
            <Input
              id={`${formId}-actor`}
              value={actor}
              onChange={(event) => setActor(event.target.value)}
              maxLength={191}
              placeholder="Any actor"
            />
          </div>
        )}
        <div className="space-y-2">
          <Label htmlFor={`${formId}-start`}>From (Beijing time)</Label>
          <Input
            id={`${formId}-start`}
            type="datetime-local"
            value={start}
            onChange={(event) => setStart(event.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-end`}>Until (exclusive, Beijing time)</Label>
          <Input
            id={`${formId}-end`}
            type="datetime-local"
            value={end}
            onChange={(event) => setEnd(event.target.value)}
          />
        </div>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">
          Choose both times for a window of up to 31 days. Blank times show the latest 31 days.
        </p>
        <Button type="submit" variant="outline">
          <Search />
          Apply filters
        </Button>
      </div>
    </form>
  );
}

function EventDetails({ event, onClose }: { event: OperationEvent; onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Operation event</DialogTitle>
          <DialogDescription>
            {eventLabels[event.action] ?? event.action} at {managementTimestamp(event.created_at)} (Beijing)
          </DialogDescription>
        </DialogHeader>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-5 gap-y-3 text-sm">
          <dt className="text-muted-foreground">Event ID</dt>
          <dd className="flex min-w-0 items-center gap-2">
            <span className="break-all font-mono text-xs">{event.id}</span>
            <CopyButton value={event.id} label="Copy event ID" />
          </dd>
          <dt className="text-muted-foreground">Organization</dt>
          <dd className="break-all font-mono text-xs">{event.organization_id ?? "Gateway operation"}</dd>
          <dt className="text-muted-foreground">Actor</dt>
          <dd className="break-all font-mono text-xs">{event.actor_id}</dd>
          <dt className="text-muted-foreground">Resource</dt>
          <dd className="break-all">
            {event.resource_type}: {event.resource_id}
          </dd>
          <dt className="text-muted-foreground">Result</dt>
          <dd>Succeeded</dd>
          <dt className="text-muted-foreground">Version</dt>
          <dd>
            {event.version_before ?? "New"} -&gt; {event.version_after ?? "Not recorded"}
          </dd>
        </dl>
        <section className="space-y-3">
          <h3 className="text-sm font-medium">Recorded changes</h3>
          {Object.keys(event.changes).length === 0 ? (
            <p className="text-sm text-muted-foreground">No field changes were recorded.</p>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Field</TableHead>
                    <TableHead>Value</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {Object.entries(event.changes).map(([name, value]) => (
                    <TableRow key={name}>
                      <TableCell className="align-top font-mono text-xs">{name}</TableCell>
                      <TableCell className="max-w-72 whitespace-pre-wrap break-all font-mono text-xs">
                        {JSON.stringify(value, null, 2)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </section>
      </DialogContent>
    </Dialog>
  );
}

function useOperationAudit(filters: AuditFilters) {
  const { accessToken, userId, userRole, isViewOnly } = useAuthorized();
  const admin = isProxyAdminRole(userRole) && !isViewOnly;
  const scopeOptions = {
    queryKey: ["retail", "organizations", userId, "audit-scope", filters.scope_query, filters.scope_page],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<RetailOrganization>>(
        managementQuery("/organizations", {
          q: filters.scope_query,
          limit: managementPageSize,
          offset: (filters.scope_page - 1) * managementPageSize,
        }),
        accessToken,
        { signal },
      ),
    enabled: Boolean(accessToken) && !admin,
    retry: false,
  };
  const organizations = useQuery(scopeOptions);
  const allowed = (organizations.error ? [] : organizations.data?.items ?? []).filter(
    (organization) => organization.can_view_events,
  );
  const scoped = allowed.find((organization) => organization.id === filters.organization);
  const mayRead = admin || Boolean(scoped);
  const path = admin
    ? "/admin/operation-events"
    : `/organizations/${encodeURIComponent(scoped?.id ?? "")}/operation-events`;
  const queryParameters = {
    organization_id: admin ? filters.organization : undefined,
    action: filters.action,
    actor_id: admin ? filters.actor : undefined,
    start: filters.start,
    end: filters.end,
    limit: managementPageSize,
    offset: (filters.page - 1) * managementPageSize,
  };
  const eventOptions = {
    queryKey: ["retail", "operation-events", userId, admin ? "admin" : "organization", filters],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<OperationEvent>>(managementQuery(path, queryParameters), accessToken, { signal }),
    enabled: Boolean(accessToken) && mayRead,
    retry: false,
  };
  const events = useQuery(eventOptions);
  return { admin, organizations, allowed, scoped, mayRead, events };
}

function OperationEvents({
  events,
  page,
  onPage,
  onSelect,
}: {
  events: UseQueryResult<PageResult<OperationEvent>, Error>;
  page: number;
  onPage: (page: number) => void;
  onSelect: (event: OperationEvent) => void;
}) {
  return (
    <section className="overflow-hidden rounded-xl border" aria-label="Operation events">
      {events.isPending && (
        <LoadingState description="Loading authorized gateway data." title="Loading operation events" />
      )}
      {events.error && <ManagementError title="Unable to load operation events" error={events.error} />}
      {events.data && !events.error && (
        <>
          {events.data.items.length === 0 ? (
            <StateMessage
              role="status"
              icon={<ClipboardList className="size-5" />}
              title="No operation events found"
              description="Recorded changes will appear here when they match the selected scope and time window."
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-5">Time (Beijing)</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Actor</TableHead>
                  <TableHead>Resource</TableHead>
                  <TableHead className="pr-5">
                    <span className="sr-only">Details</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.data.items.map((event) => (
                  <TableRow key={event.id}>
                    <TableCell className="pl-5 text-xs text-muted-foreground">
                      {managementTimestamp(event.created_at)}
                    </TableCell>
                    <TableCell>{eventLabels[event.action] ?? event.action}</TableCell>
                    <TableCell className="max-w-40 truncate font-mono text-xs">{event.actor_id}</TableCell>
                    <TableCell className="max-w-52 truncate font-mono text-xs">
                      {event.resource_type}: {event.resource_id}
                    </TableCell>
                    <TableCell className="pr-5 text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => onSelect(event)}
                        aria-label={`View event ${event.id}`}
                      >
                        Details
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <ManagementPagination page={page} total={events.data.total} loading={events.isFetching} onChange={onPage} />
        </>
      )}
    </section>
  );
}

function scopeDescription(allowed: number, total: number): string {
  if (total === 0) return "No associated organizations match your search. Try another name.";
  if (allowed === 0) return "No organizations on this page grant audit access. Search by name or choose another page.";
  return "Select an organization you administer and apply the filters to view its recorded operations.";
}

function AuditScopePanel({
  admin,
  organizations,
  allowed,
  scoped,
  filters,
  onSearch,
  onPage,
  onApply,
}: {
  admin: boolean;
  organizations: UseQueryResult<PageResult<RetailOrganization>, Error>;
  allowed: RetailOrganization[];
  scoped: RetailOrganization | undefined;
  filters: AuditFilters;
  onSearch: (query: string) => void;
  onPage: (page: number) => void;
  onApply: (filters: AppliedAuditFilters) => void;
}) {
  const showFilters = admin || (organizations.data && !organizations.error);
  const scopeLoaded = organizations.data && !organizations.error;
  const chooseScope = !admin && scopeLoaded && !scoped;
  return (
    <>
      {!admin && (
        <AuditOrganizationSearch
          query={filters.scope_query}
          page={filters.scope_page}
          total={organizations.error ? 0 : organizations.data?.total ?? 0}
          fetching={organizations.isFetching}
          hasResults={Boolean(organizations.data && !organizations.error)}
          onSearch={onSearch}
          onPage={onPage}
        />
      )}
      {!admin && organizations.error && (
        <ManagementError title="Unable to load your organizations" error={organizations.error} />
      )}
      {!admin && organizations.isPending && (
        <LoadingState description="Loading authorized gateway data." title="Loading your audit scope" />
      )}
      {showFilters && (
        <AuditFiltersForm
          key={JSON.stringify(filters)}
          filters={filters}
          admin={admin}
          organizations={allowed}
          onApply={onApply}
        />
      )}
      {chooseScope && (
        <StateMessage
          role="status"
          icon={<ClipboardList className="size-5" />}
          title="Choose an authorized organization"
          description={scopeDescription(allowed.length, organizations.data?.total ?? 0)}
        />
      )}
    </>
  );
}

function OperationAuditWorkspace() {
  const [filters, setFilters] = useQueryStates(auditParsers);
  const [selected, setSelected] = useState<OperationEvent | null>(null);
  const { admin, organizations, allowed, scoped, mayRead, events } = useOperationAudit(filters);
  const detailsInScope = admin || selected?.organization_id === scoped?.id;
  const validDetails = selected && mayRead && !events.error;
  const showDetails = validDetails && detailsInScope;
  const changeScopeSearch = (query: string) => {
    setSelected(null);
    const next = { scope_query: query, scope_page: 1, organization: "", page: 1 };
    void setFilters(next);
  };
  const changeScopePage = (page: number) => {
    setSelected(null);
    void setFilters({ scope_page: page, organization: "", page: 1 });
  };

  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <ClipboardList />
            Operation Audit
          </PageHeaderTitle>
          <PageHeaderDescription>
            Recorded organization, membership, and report operations, with actor IDs and change history.
          </PageHeaderDescription>
        </PageHeader>
        <Button variant="outline" disabled={!mayRead || events.isFetching} onClick={() => void events.refetch()}>
          <RefreshCw />
          Refresh events
        </Button>
      </div>
      <AuditScopePanel
        admin={admin}
        organizations={organizations}
        allowed={allowed}
        scoped={scoped}
        filters={filters}
        onSearch={changeScopeSearch}
        onPage={changeScopePage}
        onApply={(next) => {
          setSelected(null);
          void setFilters({ ...next, page: 1 });
        }}
      />
      {mayRead && (
        <OperationEvents
          events={events}
          page={filters.page}
          onPage={(page) => void setFilters({ page })}
          onSelect={setSelected}
        />
      )}
      <Alert variant="info">
        <AlertDescription>
          This view contains recorded management operations. Request traces and wallet transactions are available in
          Logs and Billing.
        </AlertDescription>
      </Alert>
      {showDetails && selected && <EventDetails event={selected} onClose={() => setSelected(null)} />}
    </>
  );
}

export default function RetailOperationAuditPanel() {
  return (
    <Page>
      <ManagementFeatureGate feature="operation_audit_enabled" title="Operation audit">
        <OperationAuditWorkspace />
      </ManagementFeatureGate>
    </Page>
  );
}
