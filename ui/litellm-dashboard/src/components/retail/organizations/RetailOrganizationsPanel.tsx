"use client";

import { useQuery } from "@tanstack/react-query";
import { Building2, Plus, RefreshCw, Search, Users } from "lucide-react";
import Link from "next/link";
import { useQueryStates } from "nuqs";
import { useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import CopyButton from "@/components/shared/CopyButton";
import { LoadingState } from "@/components/shared/LoadingState";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { uiHref } from "@/utils/uiHref";
import { retailRequest, type PageResult } from "../api";
import {
  ManagementError,
  ManagementFeatureGate,
  ManagementPagination,
  managementPageParser,
  managementPageSize,
  managementQuery,
  managementTextParser,
  managementTimestamp,
} from "../management/ManagementShared";
import OrganizationEditor from "./OrganizationEditor";
import OrganizationMembers from "./OrganizationMembers";
import type { RetailOrganization } from "./types";
import { organizationFinanceEnabled, useOrganizationFinanceConfiguration } from "../organization-finance/FinanceShared";
import { DirectoryFinanceNotice, useDirectoryFinance } from "../organization-finance/delegated/DelegatedShared";

const organizationParsers = { q: managementTextParser, page: managementPageParser };

function organizationRoleLabel(role: RetailOrganization["member_role"], admin: boolean): string {
  if (role === "admin") return "Organization admin";
  if (role === "member") return "Member";
  return admin ? "Gateway administrator" : "Not a member";
}

function OrganizationRow({
  organization,
  admin,
  financeEnabled,
  delegatedFinance,
  onMembers,
  onEdit,
}: {
  organization: RetailOrganization;
  admin: boolean;
  financeEnabled: boolean;
  delegatedFinance: boolean;
  onMembers: (organization: RetailOrganization) => void;
  onEdit: (organization: RetailOrganization) => void;
}) {
  return (
    <TableRow>
      <TableCell className="pl-5">
        <p className="font-medium">{organization.name}</p>
        <div className="mt-1 flex items-center gap-2">
          <span className="max-w-56 truncate font-mono text-xs text-muted-foreground">{organization.id}</span>
          <CopyButton value={organization.id} label={`Copy organization ID ${organization.name}`} />
        </div>
      </TableCell>
      <TableCell>
        <Badge variant={organization.enabled ? "secondary" : "outline"}>
          {organization.enabled ? "Enabled" : "Disabled"}
        </Badge>
      </TableCell>
      <TableCell>{organizationRoleLabel(organization.member_role, admin)}</TableCell>
      <TableCell className="text-xs text-muted-foreground">{managementTimestamp(organization.updated_at)}</TableCell>
      <TableCell className="pr-5">
        <div className="flex flex-wrap justify-end gap-2">
          {!admin && delegatedFinance && (
            <Link
              className="inline-flex h-8 items-center rounded-md border px-3 text-sm hover:bg-muted"
              href={`${uiHref("organization-finance")}?organization=${encodeURIComponent(organization.id)}`}
            >
              Finance
            </Link>
          )}
          {admin && financeEnabled && (
            <Link
              className="inline-flex h-8 items-center rounded-md border px-3 text-sm hover:bg-muted"
              href={`${uiHref("organization-consumption")}?organization=${encodeURIComponent(organization.id)}`}
            >
              Consumption
            </Link>
          )}
          {(organization.can_manage_members || organization.can_associate_members) && (
            <Button
              variant="outline"
              size="sm"
              aria-label={`View members of ${organization.name}`}
              onClick={() => onMembers(organization)}
            >
              <Users />
              Members
            </Button>
          )}
          {organization.can_view_events && (
            <Link
              className="inline-flex h-8 items-center rounded-md border px-3 text-sm hover:bg-muted"
              href={`${uiHref("operation-audit")}?organization=${encodeURIComponent(organization.id)}`}
            >
              Audit
            </Link>
          )}
          {admin && organization.can_edit && (
            <Button
              variant="outline"
              size="sm"
              aria-label={`Edit organization ${organization.name}`}
              onClick={() => onEdit(organization)}
            >
              Edit
            </Button>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

function OrganizationsWorkspace() {
  const { accessToken, userId, userRole, isViewOnly } = useAuthorized();
  const admin = canUseAdminControlPlane({ userRole, isViewOnly });
  const financeConfig = useOrganizationFinanceConfiguration(admin);
  const financeEnabled = organizationFinanceEnabled(financeConfig.data, financeConfig.error, admin);
  const [filters, setFilters] = useQueryStates(organizationParsers);
  const [editing, setEditing] = useState<{ organization: RetailOrganization | null } | null>(null);
  const [members, setMembers] = useState<RetailOrganization | null>(null);
  const path = admin ? "/admin/organizations" : "/organizations";
  const organizationOptions = {
    queryKey: ["retail", "organizations", userId, admin ? "admin" : "member", filters.q, filters.page],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<RetailOrganization>>(
        managementQuery(path, {
          q: filters.q,
          limit: managementPageSize,
          offset: (filters.page - 1) * managementPageSize,
        }),
        accessToken,
        { signal },
      ),
    enabled: Boolean(accessToken),
    retry: false,
  };
  const organizations = useQuery(organizationOptions);
  const delegated = useDirectoryFinance(organizations.data?.items, organizations.error, admin);
  const refresh = () => {
    void organizations.refetch();
    delegated.refresh();
  };
  const mayCreate = admin && organizations.data && !organizations.error;
  const showEditor = !organizations.error ? editing : null;
  const showMembers = !organizations.error ? members : null;
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Building2 />
            Organizations
          </PageHeaderTitle>
          <PageHeaderDescription>
            {admin
              ? "Associate existing accounts, delegate organization access, and review membership changes."
              : "Organizations associated with your account. Each organization shows the actions available to your membership."}
          </PageHeaderDescription>
        </PageHeader>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={refresh} disabled={organizations.isFetching}>
            <RefreshCw />
            Refresh
          </Button>
          {mayCreate && (
            <Button onClick={() => setEditing({ organization: null })}>
              <Plus />
              Create organization
            </Button>
          )}
        </div>
      </div>
      <DirectoryFinanceNotice error={delegated.error} />
      <div className="rounded-lg border bg-muted/20 px-4 py-3 text-sm leading-relaxed text-muted-foreground">
        Organization roles manage membership inside their own organization. Personal wallets, API keys, and
        resource-pool permissions keep their existing account scope.
      </div>
      <section className="overflow-hidden rounded-xl border" aria-label="Organization directory">
        <div className="border-b p-4">
          <div className="relative max-w-md">
            <Search className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" />
            <Input
              aria-label="Search organizations"
              placeholder="Search organization names..."
              className="pl-9"
              value={filters.q}
              maxLength={100}
              onChange={(event) => void setFilters({ q: event.target.value, page: 1 })}
            />
          </div>
        </div>
        {organizations.isPending && (
          <LoadingState description="Loading authorized gateway data." title="Loading organizations" />
        )}
        {organizations.error && <ManagementError title="Unable to load organizations" error={organizations.error} />}
        {organizations.data && !organizations.error && (
          <>
            {organizations.data.items.length === 0 ? (
              <StateMessage
                role="status"
                icon={<Building2 className="size-5" />}
                title="No organizations found"
                description={
                  admin
                    ? "Create an organization and associate its first administrator using an existing account ID."
                    : "A gateway administrator can associate your existing account with an organization."
                }
              />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-5">Organization</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Your role</TableHead>
                    <TableHead>Updated (Beijing)</TableHead>
                    <TableHead className="pr-5">
                      <span className="sr-only">Actions</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {organizations.data.items.map((organization) => (
                    <OrganizationRow
                      key={organization.id}
                      organization={organization}
                      admin={admin}
                      financeEnabled={financeEnabled}
                      delegatedFinance={organization.member_role === "admin" && delegated.ids.has(organization.id)}
                      onMembers={setMembers}
                      onEdit={(organization) => setEditing({ organization })}
                    />
                  ))}
                </TableBody>
              </Table>
            )}
            <ManagementPagination
              page={filters.page}
              total={organizations.data.total}
              loading={organizations.isFetching}
              onChange={(page) => void setFilters({ page })}
            />
          </>
        )}
      </section>
      {showEditor && accessToken && (
        <OrganizationEditor
          accessToken={accessToken}
          organization={showEditor.organization}
          onClose={() => setEditing(null)}
        />
      )}
      {showMembers && accessToken && (
        <OrganizationMembers
          organization={showMembers}
          accessToken={accessToken}
          actorId={userId}
          onClose={() => setMembers(null)}
          onOrganizationChange={setMembers}
        />
      )}
    </>
  );
}

export default function RetailOrganizationsPanel() {
  return (
    <Page>
      <ManagementFeatureGate feature="organization_management_enabled" title="Organization management">
        <OrganizationsWorkspace />
      </ManagementFeatureGate>
    </Page>
  );
}
