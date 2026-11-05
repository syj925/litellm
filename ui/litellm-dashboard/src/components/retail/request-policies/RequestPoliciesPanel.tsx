"use client";

import { RefreshCw, ShieldCheck, Plus } from "lucide-react";
import { parseAsStringLiteral, useQueryStates } from "nuqs";
import { useCallback, useState } from "react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { LoadingState } from "@/components/shared/LoadingState";
import { Page, PageTabs, PageTabsList, PageTabsTrigger, PageTabsContent } from "@/components/shared/Page";
import { PageHeader, PageHeaderTitle, PageHeaderDescription } from "@/components/shared/PageHeader";
import { StateMessage } from "@/components/shared/StateMessage";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { canUseAdminControlPlane } from "@/utils/dashboardAccess";
import type { PageResult } from "../api";
import {
  ManagementError,
  ManagementFeatureGate,
  ManagementPagination,
  managementPageParser,
  managementConflict,
  managementTimestamp,
} from "../management/ManagementShared";
import { usePolicyWrite, usePrivateResource } from "./api";
import BindingEditor from "./BindingEditor";
import PolicyEditor from "./PolicyEditor";
import PromptEditor from "./PromptEditor";
import { PolicyWriteError } from "./PolicyShared";
import type { PolicyBinding, PolicyResource, PolicySession, PolicyTab, RequestPolicy, SavedPrompt } from "./types";
import { targetKindLabels } from "./types";

const tabs = ["policies", "prompts", "bindings"] as const;
const parsers = { tab: parseAsStringLiteral(tabs).withDefault("policies"), page: managementPageParser };
const names = { policies: "policy", prompts: "prompt", bindings: "binding" } as const;
function resourceName(row: PolicyResource): string {
  return "name" in row ? row.name : row.target_label;
}
function resourceStatus(tab: PolicyTab, enabled: boolean): string {
  if (tab === "bindings") return enabled ? "Applied" : "Not applied";
  return enabled ? "Enabled" : "Paused";
}
function CollectionEditor({
  tab,
  row,
  session,
  onClose,
  onSaved,
}: {
  tab: PolicyTab;
  row: PolicyResource | null;
  session: PolicySession;
  onClose: () => void;
  onSaved: () => void;
}) {
  if (tab === "policies")
    return <PolicyEditor session={session} policy={row as RequestPolicy | null} onClose={onClose} onSaved={onSaved} />;
  if (tab === "prompts")
    return <PromptEditor session={session} prompt={row as SavedPrompt | null} onClose={onClose} onSaved={onSaved} />;
  return <BindingEditor session={session} binding={row as PolicyBinding | null} onClose={onClose} onSaved={onSaved} />;
}
const collectionDescriptions = {
  policies: "Restrict routes and output size, set request parameters, and attach a saved prompt.",
  prompts: "Reusable private prompts. The list contains metadata only; choose Edit to read prompt content.",
  bindings:
    "Apply one policy to a public model, Team, user, or virtual key. All matching restrictions combine in that order.",
} as const;
function DeleteResource({
  tab,
  row,
  session,
  onClose,
  onSaved,
}: {
  tab: PolicyTab;
  row: PolicyResource;
  session: PolicySession;
  onClose: () => void;
  onSaved: () => void;
}) {
  const write = usePolicyWrite(session, onSaved);
  return (
    <section aria-label="Confirm deletion" className="space-y-4 rounded-lg border p-5">
      <h2 className="text-lg font-semibold">
        Delete {names[tab]} “{resourceName(row)}”?
      </h2>
      <p className="max-w-prose text-sm text-muted-foreground">
        This cannot be undone. Referenced prompts and policies must be unassigned before deletion. Deleting a binding
        stops applying its restriction.
      </p>
      <PolicyWriteError error={write.error} />
      <div className="flex gap-2">
        <Button
          variant="destructive"
          onClick={() => void write.run(`/${tab}/${row.id}?version=${row.version}`, "DELETE")}
          disabled={write.pending || managementConflict(write.error)}
        >
          {write.pending ? "Deleting…" : "Delete permanently"}
        </Button>
        <Button variant="outline" onClick={onClose} disabled={write.pending}>
          Cancel
        </Button>
      </div>
    </section>
  );
}
function Collection({
  tab,
  page,
  session,
  onPage,
}: {
  tab: PolicyTab;
  page: number;
  session: PolicySession;
  onPage: (page: number) => void;
}) {
  const [revision, setRevision] = useState(0);
  const [editor, setEditor] = useState<{ row: PolicyResource | null } | null>(null);
  const [deleting, setDeleting] = useState<PolicyResource | null>(null);
  const list = usePrivateResource<PageResult<PolicyResource>>(
    `/${tab}?limit=25&offset=${(page - 1) * 25}`,
    session,
    revision,
  );
  const editingResource = Boolean(editor) || Boolean(deleting);
  const refresh = () => {
    setEditor(null);
    setDeleting(null);
    setRevision((value) => value + 1);
  };
  const changePage = (next: number) => {
    setEditor(null);
    setDeleting(null);
    onPage(next);
  };
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-sm text-muted-foreground">{collectionDescriptions[tab]}</p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={refresh} disabled={Boolean(editor) || Boolean(deleting)}>
            <RefreshCw />
            Refresh
          </Button>
          <Button
            onClick={() => {
              setDeleting(null);
              setEditor({ row: null });
            }}
            disabled={!list.data || Boolean(list.error) || editingResource}
          >
            <Plus />
            Create {names[tab]}
          </Button>
        </div>
      </div>
      {!list.error && editor && (
        <CollectionEditor
          tab={tab}
          row={editor.row}
          session={session}
          onClose={() => setEditor(null)}
          onSaved={refresh}
        />
      )}
      {!list.error && deleting && (
        <DeleteResource
          tab={tab}
          row={deleting}
          session={session}
          onClose={() => setDeleting(null)}
          onSaved={refresh}
        />
      )}
      {list.error && <ManagementError title={`Unable to load ${tab}`} error={list.error} />}
      {!list.error && !list.data && (
        <LoadingState title={`Loading ${tab}`} description="Loading authorized gateway configuration." />
      )}
      {!list.error && list.data && (
        <section className="overflow-hidden rounded-lg border" aria-label={`Saved ${tab}`}>
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{tab === "bindings" ? "Target" : "Name"}</TableHead>
                  {tab === "bindings" && <TableHead>Policy</TableHead>}
                  <TableHead>Status</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {list.data.items.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={tab === "bindings" ? 5 : 4} className="py-10 text-center text-muted-foreground">
                      No {tab} yet. Create a {names[tab]} to get started.
                    </TableCell>
                  </TableRow>
                ) : (
                  list.data.items.map((row) => (
                    <TableRow key={row.id}>
                      <TableCell className="max-w-80 break-words font-medium">
                        {resourceName(row)}
                        {"target_kind" in row && (
                          <p className="text-xs font-normal text-muted-foreground">
                            {targetKindLabels[row.target_kind]}
                          </p>
                        )}
                      </TableCell>
                      {"policy_name" in row && <TableCell>{row.policy_name}</TableCell>}
                      <TableCell>
                        <Badge variant={row.enabled ? "secondary" : "outline"}>
                          {resourceStatus(tab, row.enabled)}
                        </Badge>
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                        {managementTimestamp(row.updated_at)}
                      </TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-2">
                          <Button
                            size="sm"
                            variant="outline"
                            aria-label={`Edit ${resourceName(row)}`}
                            disabled={Boolean(editor) || Boolean(deleting)}
                            onClick={() => setEditor({ row })}
                          >
                            Edit
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            aria-label={`Delete ${resourceName(row)}`}
                            disabled={Boolean(editor) || Boolean(deleting)}
                            onClick={() => setDeleting(row)}
                          >
                            Delete
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </div>
          <ManagementPagination
            page={page}
            total={list.data.total}
            loading={Boolean(editor) || Boolean(deleting)}
            onChange={changePage}
          />
        </section>
      )}
    </div>
  );
}
function RequestPoliciesWorkspace({ token }: { token: string }) {
  const [filters, setFilters] = useQueryStates(parsers);
  const [denied, setDenied] = useState(false);
  const onDenied = useCallback(() => setDenied(true), []);
  if (denied)
    return (
      <StateMessage
        role="alert"
        icon={<ShieldCheck />}
        title="Administrator access is no longer available"
        description="Private configuration has been cleared. Sign in with a gateway administrator account to continue."
      />
    );
  const session = { token, onDenied };
  return (
    <>
      <PageHeader>
        <PageHeaderTitle>
          <ShieldCheck />
          Request policies
        </PageHeaderTitle>
        <PageHeaderDescription>
          Control supported API requests with independent gateway policies and reusable prompts.
        </PageHeaderDescription>
      </PageHeader>
      <div className="rounded-lg border bg-muted/20 px-4 py-3 text-sm leading-relaxed text-muted-foreground">
        Pausing a bound policy or prompt blocks matching requests. To stop applying restrictions, turn off “Apply
        policy” on its binding. These controls do not change native model permissions, budgets, or wallet balances.
      </div>
      <PageTabs
        value={filters.tab}
        onValueChange={(value) => {
          if (tabs.includes(value as PolicyTab)) void setFilters({ tab: value as PolicyTab, page: 1 });
        }}
      >
        <PageTabsList aria-label="Request policy sections">
          {tabs.map((tab) => (
            <PageTabsTrigger key={tab} value={tab}>
              {tab[0].toUpperCase() + tab.slice(1)}
            </PageTabsTrigger>
          ))}
        </PageTabsList>
        {tabs.map((tab) => (
          <PageTabsContent key={tab} value={tab}>
            {filters.tab === tab && (
              <Collection
                key={`${tab}-${filters.page}`}
                tab={tab}
                page={filters.page}
                session={session}
                onPage={(page) => void setFilters({ page })}
              />
            )}
          </PageTabsContent>
        ))}
      </PageTabs>
    </>
  );
}
export default function RequestPoliciesPanel() {
  const auth = useAuthorized();
  if (auth.isLoading)
    return (
      <Page>
        <LoadingState title="Loading account" description="Checking gateway administrator access." />
      </Page>
    );
  if (!auth.isAuthorized || !auth.accessToken || !canUseAdminControlPlane(auth))
    return (
      <Page>
        <StateMessage
          role="alert"
          icon={<ShieldCheck />}
          title="Gateway administrator access required"
          description="Request policies and saved prompts are private to gateway administrators."
        />
      </Page>
    );
  return (
    <Page>
      <ManagementFeatureGate feature="request_policies_enabled" title="Request policies">
        <RequestPoliciesWorkspace key={`${auth.userId}:${auth.accessToken}`} token={auth.accessToken} />
      </ManagementFeatureGate>
    </Page>
  );
}
