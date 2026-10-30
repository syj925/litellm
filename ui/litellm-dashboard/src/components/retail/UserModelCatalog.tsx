"use client";

import { useState } from "react";
import Link from "next/link";
import { Boxes, RefreshCw, Search } from "lucide-react";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { useKeyResourceCatalog } from "@/components/organisms/useKeyResourceCatalog";
import { resourcePolicyPoolModels } from "@/components/organisms/resourceRoutingPolicy";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { uiHref } from "@/utils/uiHref";
import { sourceKindLabel } from "./pools/types";

export default function UserModelCatalog() {
  const { accessToken, userId } = useAuthorized();
  const catalog = useKeyResourceCatalog({ accessToken, targetUserId: userId });
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const rows = resourcePolicyPoolModels({ mode: "auto" }, catalog.data?.items ?? []).flatMap(({ pool, models }) =>
    models.map((model) => ({ model, pool })),
  );
  const visible = rows.filter(({ model, pool }) => `${model} ${pool.name}`.toLowerCase().includes(needle));

  return (
    <div className="flex flex-col gap-6 p-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <PageHeader>
          <PageHeaderTitle>
            <Boxes />
            Models
          </PageHeaderTitle>
          <PageHeaderDescription>Models and resource pools available to your account.</PageHeaderDescription>
        </PageHeader>
        <Button variant="outline" disabled={catalog.isFetching} onClick={() => void catalog.refetch()}>
          <RefreshCw />
          Refresh
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <Link href={uiHref("api-keys")} className="text-sm font-medium hover:underline">
          Manage your API keys
        </Link>
        <Link href={uiHref("playground")} className="text-sm font-medium hover:underline">
          Open playground
        </Link>
      </div>
      {catalog.isPending && (
        <LoadingState title="Loading available models" description="Checking your account permissions." />
      )}
      {catalog.error && (
        <Alert variant="error">
          <AlertTitle>Unable to verify model access</AlertTitle>
          <AlertDescription>{catalog.error.message}</AlertDescription>
        </Alert>
      )}
      {catalog.data && !catalog.error && (
        <>
          <div className="relative max-w-md">
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground"
            />
            <Input
              aria-label="Search available models"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search model or resource pool"
              className="pl-9"
            />
          </div>
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Model name</TableHead>
                  <TableHead>Resource pool</TableHead>
                  <TableHead>Source</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map(({ model, pool }) => (
                  <TableRow key={`${pool.id}:${model}`}>
                    <TableCell className="font-mono text-sm">{model}</TableCell>
                    <TableCell>{pool.name}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{sourceKindLabel(pool.source_kind ?? "custom")}</Badge>
                    </TableCell>
                  </TableRow>
                ))}
                {!visible.length && (
                  <TableRow>
                    <TableCell colSpan={3} className="py-12 text-center text-muted-foreground">
                      {rows.length
                        ? "No models match your search."
                        : "No models are currently available to your account. Contact your administrator for access."}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
          <p className="text-sm text-muted-foreground">
            This is your account access. Each API key can apply additional resource pool and model limits.
          </p>
        </>
      )}
    </div>
  );
}
