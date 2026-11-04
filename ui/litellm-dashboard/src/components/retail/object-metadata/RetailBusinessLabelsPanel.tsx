"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Search, Tags } from "lucide-react";
import { useQueryStates } from "nuqs";
import { useCallback, useId, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import CopyButton from "@/components/shared/CopyButton";
import { LoadingState } from "@/components/shared/LoadingState";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { retailRequest, type PageResult } from "../api";
import {
  ManagementError,
  ManagementFeatureGate,
  ManagementPagination,
  managementPageSize,
  managementQuery,
} from "../management/ManagementShared";
import { businessLabelsParsers } from "./businessLabels";
import BusinessLabelsEditor from "./BusinessLabelsEditor";
import { objectKindLabels, objectMetadataKinds, type MetadataTarget, type ObjectMetadataKind } from "./types";

type TargetPage = PageResult<MetadataTarget> & { scope_note: string };

function TargetRow({
  target,
  selected,
  onSelect,
}: {
  target: MetadataTarget;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <TableRow data-state={selected ? "selected" : undefined}>
      <TableCell className="pl-5">
        <p className="break-words font-medium">{target.name}</p>
        <div className="mt-1 flex items-center gap-2">
          <span className="font-mono text-xs text-muted-foreground">{target.id.slice(0, 8)}</span>
          <CopyButton value={target.id} label={`Copy object reference for ${target.name}`} />
        </div>
      </TableCell>
      <TableCell>
        <div className="flex max-w-sm flex-wrap gap-1">
          {target.metadata.tags.length ? (
            target.metadata.tags.map((tag) => (
              <Badge key={tag} variant="outline" className="max-w-full break-all whitespace-normal">
                {tag}
              </Badge>
            ))
          ) : (
            <span className="text-xs text-muted-foreground">No labels</span>
          )}
        </div>
      </TableCell>
      <TableCell className="pr-5 text-right">
        <Button
          variant={selected ? "secondary" : "outline"}
          size="sm"
          aria-label={`Edit labels for ${target.name}`}
          onClick={onSelect}
        >
          Edit labels
        </Button>
      </TableCell>
    </TableRow>
  );
}

function BusinessLabelsWorkspace() {
  const { accessToken, userId } = useAuthorized();
  const id = useId();
  const [filters, setFilters] = useQueryStates(businessLabelsParsers);
  const [authorityLost, setAuthorityLost] = useState(false);
  const targetsKey = ["retail", "object-metadata", userId, "targets", filters.kind, filters.q, filters.page];
  const queryParameters = {
    kind: filters.kind,
    q: filters.q,
    limit: managementPageSize,
    offset: (filters.page - 1) * managementPageSize,
  };
  const options = {
    queryKey: targetsKey,
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<TargetPage>(managementQuery("/admin/object-metadata/targets", queryParameters), accessToken, {
        signal,
      }),
    retry: false,
  };
  const targets = useQuery(options);
  const visible = !targets.error && !authorityLost ? targets.data : undefined;
  const selected = visible?.items.find((target) => target.id === filters.target);
  const clearSelection = useCallback(() => {
    setAuthorityLost(true);
    void setFilters({ target: null });
  }, [setFilters]);
  const refresh = async () => {
    const result = await targets.refetch();
    if (!result.error) setAuthorityLost(false);
  };
  const changeKind = (kind: ObjectMetadataKind) => {
    const next = { kind, q: "", page: 1, target: null };
    void setFilters(next);
  };
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Tags />
            Business labels
          </PageHeaderTitle>
          <PageHeaderDescription>
            Classify supported API keys, teams, organizations, and model deployments.
          </PageHeaderDescription>
        </PageHeader>
        <Button variant="outline" disabled={targets.isFetching} onClick={() => void refresh()}>
          <RefreshCw />
          Refresh objects
        </Button>
      </div>
      <p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">
        Business labels are control-plane categories. They currently do not change authorization, routing, budgets,
        billing, or Usage reporting.
      </p>
      <div className="grid items-start gap-6 xl:grid-cols-5">
        <section className="overflow-hidden rounded-xl border xl:col-span-3" aria-label="Objects available for labels">
          <div className="grid gap-4 border-b p-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor={`${id}-kind`}>Object type</Label>
              <select
                id={`${id}-kind`}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                value={filters.kind}
                onChange={(event) => {
                  const kind = event.target.value;
                  if (objectMetadataKinds.includes(kind as ObjectMetadataKind)) changeKind(kind as ObjectMetadataKind);
                }}
              >
                {objectMetadataKinds.map((kind) => (
                  <option key={kind} value={kind}>
                    {objectKindLabels[kind]}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${id}-search`}>Search object names</Label>
              <div className="relative">
                <Search className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" />
                <Input
                  id={`${id}-search`}
                  className="pl-9"
                  value={filters.q}
                  maxLength={100}
                  placeholder="Search by name"
                  onChange={(event) => void setFilters({ q: event.target.value, page: 1, target: null })}
                />
              </div>
            </div>
          </div>
          {targets.isPending && (
            <LoadingState
              title="Loading objects"
              description="Checking supported objects available to this administrator."
            />
          )}
          {targets.error && <ManagementError title="Unable to load objects" error={targets.error} />}
          {authorityLost && (
            <StateMessage
              role="alert"
              icon={<Tags className="size-5" />}
              title="Object access changed"
              description="The selected object is no longer available with your current access. Refresh objects to verify your current scope."
            />
          )}
          {visible && (
            <>
              <p className="border-b px-5 py-3 text-xs leading-relaxed text-muted-foreground">{visible.scope_note}</p>
              {visible.items.length === 0 ? (
                <StateMessage
                  role="status"
                  icon={<Tags className="size-5" />}
                  title="No supported objects found"
                  description="Try another object type or search by name. Business labels apply to supported existing objects."
                />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-5">Object</TableHead>
                      <TableHead>Business labels</TableHead>
                      <TableHead className="pr-5">
                        <span className="sr-only">Actions</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {visible.items.map((target) => (
                      <TargetRow
                        key={target.id}
                        target={target}
                        selected={target.id === selected?.id}
                        onSelect={() => void setFilters({ target: target.id })}
                      />
                    ))}
                  </TableBody>
                </Table>
              )}
              <ManagementPagination
                page={filters.page}
                total={Math.min(visible.total, 10_025)}
                loading={targets.isFetching}
                onChange={(page) => void setFilters({ page, target: null })}
              />
              {visible.total > 10_025 && (
                <p role="status" className="border-t px-5 py-3 text-sm leading-relaxed text-muted-foreground">
                  There are {visible.total.toLocaleString()} matching objects. Narrow your search to reach objects
                  beyond the first 10,025.
                </p>
              )}
            </>
          )}
        </section>
        <div className="xl:col-span-2">
          {selected && accessToken ? (
            <BusinessLabelsEditor
              key={`${selected.kind}:${selected.id}`}
              selected={selected}
              accessToken={accessToken}
              userId={userId}
              onUnavailable={clearSelection}
            />
          ) : (
            <StateMessage
              role="status"
              icon={<Tags className="size-5" />}
              title="Choose an object to edit labels"
              description="Select Edit labels from the supported object list. Object settings and label history are shown separately."
            />
          )}
        </div>
      </div>
    </>
  );
}

export default function RetailBusinessLabelsPanel() {
  const { userRole, isViewOnly, isAuthorized, isLoading } = useAuthorized();
  if (!isLoading && !isAuthorized) return null;
  if (isLoading)
    return (
      <Page>
        <LoadingState title="Loading business labels" description="Checking administrator access." />
      </Page>
    );
  if (!canUseAdminControlPlane({ userRole, isViewOnly }))
    return (
      <Page>
        <StateMessage
          role="alert"
          icon={<Tags className="size-5" />}
          title="Gateway administrator access required"
          description="Business labels are managed by gateway administrators. Contact your administrator if you need a classification changed."
        />
      </Page>
    );
  return (
    <Page>
      <ManagementFeatureGate feature="object_metadata_enabled" title="Business labels">
        <BusinessLabelsWorkspace />
      </ManagementFeatureGate>
    </Page>
  );
}
