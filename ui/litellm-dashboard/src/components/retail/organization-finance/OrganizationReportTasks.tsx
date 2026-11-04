"use client";

import { useMutation, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useQueryState } from "nuqs";
import { useCallback, useRef, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Button } from "@/components/ui/button";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import { retailRequest, type PageResult } from "../api";
import {
  managementAttempt,
  managementPageSize,
  managementQuery,
  type IdempotencyAttempt,
} from "../management/ManagementShared";
import { downloadOrganizationReport } from "./downloadOrganizationReport";
import { financeAccessLost } from "./financeInput";
import { organizationFinanceEnabled, useOrganizationFinanceConfiguration } from "./FinanceShared";
import OrganizationReportDetails from "./OrganizationReportDetails";
import {
  organizationExportPageParser,
  organizationReportError,
  organizationReportPayload,
  organizationReportPending,
  organizationReportRefreshNeeded,
} from "./organizationReportInput";
import { OrganizationReportList } from "./OrganizationReportsTable";
import type { CreateOrganizationReport, OrganizationConsumption, OrganizationReportJob } from "./types";

type Props = { organizationId: string; preview: OrganizationConsumption | undefined; previewLoading: boolean };

function creationLabel(pending: boolean, created: boolean): string {
  if (pending) return "Creating fixed task…";
  if (created) return "CSV task created";
  return "Create fixed CSV for displayed window";
}

function ExportCreationControls({
  disabled,
  pending,
  created,
  error,
  onCreate,
  onRestart,
}: {
  disabled: boolean;
  pending: boolean;
  created: boolean;
  error: Error | null;
  onCreate: () => void;
  onRestart: () => void;
}) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <Button disabled={disabled} onClick={onCreate}>
          {creationLabel(pending, created)}
        </Button>
        {created && (
          <Button variant="ghost" onClick={onRestart}>
            Start a new snapshot
          </Button>
        )}
        <p className="max-w-prose text-xs text-muted-foreground">Apply date changes in the preview before exporting.</p>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {organizationReportError(error)}
        </p>
      )}
    </>
  );
}

function OrganizationExportWorkspace({
  organizationId,
  preview,
  previewLoading,
  actorId,
  accessToken,
}: Props & { actorId: string | null; accessToken: string }) {
  const client = useQueryClient();
  const [page, setPage] = useQueryState("export_page", organizationExportPageParser);
  const [selected, setSelected] = useState<string | null>(null);
  const [accessRevoked, setAccessRevoked] = useState(false);
  const [createdFor, setCreatedFor] = useState<string | null>(null);
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const payload = organizationReportPayload(preview, organizationId);
  const signature = JSON.stringify(payload);
  const eligible = payload !== null;
  const path = `/admin/organizations/${encodeURIComponent(organizationId)}/reports`;
  const options = {
    queryKey: ["retail", "organization-report-jobs", actorId, organizationId, "list", page],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<PageResult<OrganizationReportJob>>(
        managementQuery(path, { limit: managementPageSize, offset: (page - 1) * managementPageSize }),
        accessToken,
        { signal },
      ),
    enabled: eligible && !accessRevoked,
    retry: false,
    refetchInterval: (query: Query<PageResult<OrganizationReportJob>>): number | false => {
      if (query.state.error) return false;
      return query.state.data?.items.some(organizationReportPending) ? 2000 : false;
    },
  };
  const jobs = useQuery(options);
  const authorityLost = accessRevoked || financeAccessLost(jobs.error);
  const refreshList = useCallback(() => {
    void client.invalidateQueries({
      queryKey: ["retail", "organization-report-jobs", actorId, organizationId, "list"],
    });
  }, [client, actorId, organizationId]);
  const unavailable = useCallback(() => {
    setAccessRevoked(true);
    setSelected(null);
  }, []);
  const createOptions = {
    mutationFn: (body: CreateOrganizationReport) =>
      retailRequest<OrganizationReportJob>(path, accessToken, {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "Idempotency-Key": managementAttempt(attempt, body) },
      }),
    onSuccess: (job: OrganizationReportJob, body: CreateOrganizationReport) => {
      attempt.current = null;
      setCreatedFor(JSON.stringify(body));
      setSelected(job.id);
      void setPage(1);
      refreshList();
    },
    onError: (error: Error) => {
      if (financeAccessLost(error)) unavailable();
      else if (organizationReportRefreshNeeded(error)) refreshList();
    },
  };
  const create = useMutation(createOptions);
  const downloadOptions = {
    mutationFn: (id: string) => downloadOrganizationReport(organizationId, id, accessToken),
    onError: (error: Error, id: string) => {
      if (financeAccessLost(error)) unavailable();
      else if (organizationReportRefreshNeeded(error)) {
        refreshList();
        void client.invalidateQueries({
          queryKey: ["retail", "organization-report-jobs", actorId, organizationId, "detail", id],
        });
      }
    },
  };
  const download = useMutation(downloadOptions);
  const visible = eligible && !authorityLost && !jobs.error ? jobs.data : undefined;
  const created = createdFor === signature;
  const busy = create.isPending || jobs.isFetching || previewLoading;
  const missingData = !payload || !visible;
  const creationDisabled = missingData || busy || created;
  const creationError = JSON.stringify(create.variables) === signature ? create.error : null;
  const reload = async () => {
    create.reset();
    download.reset();
    setSelected(null);
    const result = await jobs.refetch();
    if (!result.error) setAccessRevoked(false);
  };
  return (
    <section className="space-y-4 border-t pt-6" aria-label="Fixed organization exports">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-2">
          <h2 className="font-semibold">Fixed organization CSV exports</h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            A task freezes the displayed window and request outcomes at creation. Later settlements update the live
            preview, and leave existing CSV snapshots unchanged.
          </p>
        </div>
        {eligible && (
          <Button variant="outline" disabled={busy} onClick={() => void reload()}>
            <RefreshCw />
            Reload export tasks
          </Button>
        )}
      </div>
      {!eligible && (
        <p role="status" className="text-sm text-muted-foreground">
          Load an enabled organization’s preview to create or view fixed exports.
        </p>
      )}
      {authorityLost && (
        <p role="alert" className="text-sm text-destructive">
          Export access changed. Reload export tasks to verify your current access.
        </p>
      )}
      {eligible && !authorityLost && (
        <>
          <ExportCreationControls
            disabled={creationDisabled}
            pending={create.isPending}
            created={created}
            error={creationError}
            onCreate={() => {
              if (payload && !creationDisabled) create.mutate(payload);
            }}
            onRestart={() => {
              setCreatedFor(null);
              create.reset();
            }}
          />
          {download.error && (
            <p role="alert" className="text-sm text-destructive">
              {organizationReportError(download.error)}
            </p>
          )}
          <OrganizationReportList
            data={visible}
            loading={jobs.isPending}
            fetching={jobs.isFetching}
            error={jobs.error}
            page={page}
            downloading={download.isPending}
            onPage={(next) => void setPage(next)}
            onDetails={setSelected}
            onDownload={(job) => download.mutate(job.id)}
          />
          {visible && selected && (
            <OrganizationReportDetails
              organizationId={organizationId}
              id={selected}
              actorId={actorId}
              accessToken={accessToken}
              onClose={() => setSelected(null)}
              onUnavailable={unavailable}
              onRefreshList={refreshList}
            />
          )}
        </>
      )}
    </section>
  );
}

export default function OrganizationReportTasks(props: Props) {
  const { accessToken, userId, userRole, isViewOnly, isAuthorized } = useAuthorized();
  const canWrite = canUseAdminControlPlane({ userRole, isViewOnly });
  const config = useOrganizationFinanceConfiguration(isAuthorized && canWrite);
  const financeEnabled = organizationFinanceEnabled(config.data, config.error, canWrite);
  const exportsEnabled = financeEnabled && config.data?.report_export_enabled === true;
  if (!isAuthorized || !accessToken || !exportsEnabled) return null;
  return (
    <OrganizationExportWorkspace
      key={`${userId}:${props.organizationId}`}
      {...props}
      actorId={userId}
      accessToken={accessToken}
    />
  );
}
