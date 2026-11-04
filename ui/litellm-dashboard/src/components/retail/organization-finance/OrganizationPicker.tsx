"use client";

import { useQuery } from "@tanstack/react-query";
import { useQueryStates } from "nuqs";
import { useEffect, useId } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { retailRequest, type PageResult } from "../api";
import { ManagementPagination, managementPageSize, managementQuery } from "../management/ManagementShared";
import { assignmentPickerParsers, financeAccessLost, organizationPickerParsers } from "./financeInput";
import { FinanceError } from "./FinanceShared";
import type { OrganizationChoice } from "./types";

type Props = {
  accessToken: string;
  actorId: string | null;
  userId?: string;
  value: string;
  current?: OrganizationChoice;
  disabled?: boolean;
  refreshKey?: number;
  onSelect: (value: string) => void;
  onUnavailable: () => void;
  onReady?: (ready: boolean) => void;
};

function missingCurrentChoice(
  assignment: boolean,
  current: OrganizationChoice | undefined,
  items: OrganizationChoice[],
) {
  if (assignment || !current) return undefined;
  return items.some((item) => item.id === current.id) ? undefined : current;
}

function OrganizationOptions({
  assignment,
  current,
  items,
}: {
  assignment: boolean;
  current: OrganizationChoice | undefined;
  items: OrganizationChoice[];
}) {
  const extraOption = missingCurrentChoice(assignment, current, items);
  return (
    <>
      <option value="">Choose an organization</option>
      {assignment && <option value="clear">No organization — clear assignment</option>}
      {extraOption && (
        <option value={extraOption.id}>
          {extraOption.name}
          {extraOption.enabled === false ? " (disabled)" : ""}
        </option>
      )}
      {items.map((item) => (
        <option key={item.id} value={assignment ? `organization:${item.id}` : item.id}>
          {item.name}
          {item.enabled === false ? " (disabled)" : ""}
        </option>
      ))}
    </>
  );
}

export default function OrganizationPicker(props: Props) {
  const { accessToken, actorId, userId, value, current, disabled, refreshKey, onSelect, onUnavailable, onReady } =
    props;
  const id = useId();
  const assignment = userId !== undefined;
  const [directory, setDirectory] = useQueryStates(organizationPickerParsers);
  const [eligible, setEligible] = useQueryStates(assignmentPickerParsers);
  const q = assignment ? eligible.billing_org_q : directory.org_q;
  const page = assignment ? eligible.billing_org_page : directory.org_page;
  const path = assignment
    ? `/admin/users/${encodeURIComponent(userId)}/billing-organization/organizations`
    : "/admin/organizations";
  const choices = useQuery({
    queryKey: ["retail", "organization-finance", actorId, "picker", userId ?? "directory", q, page, refreshKey],
    queryFn: ({ signal }) =>
      retailRequest<PageResult<OrganizationChoice>>(
        managementQuery(path, { q, limit: managementPageSize, offset: (page - 1) * managementPageSize }),
        accessToken,
        { signal },
      ),
    retry: false,
  });
  const visible = !choices.error ? choices.data : undefined;
  const ready = Boolean(
    visible && (value === "clear" || visible.items.some((item) => `organization:${item.id}` === value)),
  );
  useEffect(() => onReady?.(ready), [onReady, ready]);
  useEffect(() => {
    if (financeAccessLost(choices.error, assignment)) onUnavailable();
  }, [assignment, choices.error, onUnavailable]);
  const changeFilters = (nextQ: string, nextPage: number) => {
    if (assignment) {
      onSelect("");
      void setEligible({ billing_org_q: nextQ, billing_org_page: nextPage });
    } else void setDirectory({ org_q: nextQ, org_page: nextPage });
  };
  return (
    <section
      className="space-y-3"
      aria-label={assignment ? "Eligible billing organizations" : "Organization selection"}
    >
      <div className="space-y-2">
        <Label htmlFor={`${id}-search`}>Search organizations</Label>
        <Input
          id={`${id}-search`}
          value={q}
          maxLength={100}
          disabled={disabled}
          onChange={(event) => changeFilters(event.target.value, 1)}
        />
      </div>
      {choices.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          Loading organizations…
        </p>
      )}
      {choices.error && <FinanceError error={choices.error} context={assignment ? "assignment" : "organizations"} />}
      {visible && (
        <>
          <div className="space-y-2">
            <Label htmlFor={`${id}-choice`}>{assignment ? "New billing organization" : "Organization"}</Label>
            <select
              id={`${id}-choice`}
              value={value}
              disabled={disabled || choices.isFetching}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              onChange={(event) => onSelect(event.target.value)}
            >
              <OrganizationOptions assignment={assignment} current={current} items={visible.items} />
            </select>
            {visible.items.length === 0 && (
              <p role="status" className="text-sm text-muted-foreground">
                No matching organizations.
              </p>
            )}
          </div>
          <ManagementPagination
            page={page}
            total={visible.total}
            loading={choices.isFetching || Boolean(disabled)}
            onChange={(nextPage) => changeFilters(q, nextPage)}
          />
        </>
      )}
    </section>
  );
}
