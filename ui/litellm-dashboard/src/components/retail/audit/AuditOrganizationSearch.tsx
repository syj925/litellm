"use client";

import { Search } from "lucide-react";
import { useId } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ManagementPagination } from "../management/ManagementShared";

export default function AuditOrganizationSearch({
  query,
  page,
  total,
  fetching,
  hasResults,
  onSearch,
  onPage,
}: {
  query: string;
  page: number;
  total: number;
  fetching: boolean;
  hasResults: boolean;
  onSearch: (query: string) => void;
  onPage: (page: number) => void;
}) {
  const id = useId();
  return (
    <section className="overflow-hidden rounded-xl border" aria-label="Find your audit organization">
      <div className="space-y-3 p-4">
        <div>
          <h2 className="text-sm font-medium">Find an organization</h2>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            Search your associated organizations, then choose an organization with audit access in the filters below.
          </p>
        </div>
        <div className="max-w-md space-y-2">
          <Label htmlFor={id}>Search your organizations</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" />
            <Input
              id={id}
              className="pl-9"
              value={query}
              maxLength={100}
              onChange={(event) => onSearch(event.target.value)}
              placeholder="Organization name"
            />
          </div>
        </div>
      </div>
      {hasResults && <ManagementPagination page={page} total={total} loading={fetching} onChange={onPage} />}
    </section>
  );
}
