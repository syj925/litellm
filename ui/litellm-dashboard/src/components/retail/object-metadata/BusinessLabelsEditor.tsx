"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Save } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { LoadingState } from "@/components/shared/LoadingState";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/lib/toast";
import { RetailRequestError, retailRequest } from "../api";
import {
  ManagementError,
  managementAttempt,
  managementConflict,
  managementTimestamp,
  type IdempotencyAttempt,
} from "../management/ManagementShared";
import { parseBusinessLabels } from "./businessLabels";
import type { MetadataTarget } from "./types";

const recordedTime = (value: string | null): string => (value ? managementTimestamp(value) : "Not recorded");

function LabelProvenance({ target }: { target: MetadataTarget }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-5 gap-y-2 text-xs">
      <dt className="text-muted-foreground">Label record created</dt>
      <dd>{recordedTime(target.metadata.created_at)}</dd>
      <dt className="text-muted-foreground">Label record created by</dt>
      <dd className="break-all">{target.metadata.created_by ?? "Not recorded"}</dd>
      <dt className="text-muted-foreground">Label record updated</dt>
      <dd>{recordedTime(target.metadata.updated_at)}</dd>
      <dt className="text-muted-foreground">Label record updated by</dt>
      <dd className="break-all">{target.metadata.updated_by ?? "Not recorded"}</dd>
      <dt className="text-muted-foreground">Label source</dt>
      <dd>{target.metadata.source === "management_api" ? "Management API" : "Not recorded"}</dd>
      <dt className="text-muted-foreground">Object created</dt>
      <dd>{recordedTime(target.object_created_at)}</dd>
      <dt className="text-muted-foreground">Object created by</dt>
      <dd className="break-all">{target.object_created_by ?? "Not recorded"}</dd>
      <dt className="text-muted-foreground">Object updated</dt>
      <dd>{recordedTime(target.object_updated_at)}</dd>
      <dt className="text-muted-foreground">Object updated by</dt>
      <dd className="break-all">{target.object_updated_by ?? "Not recorded"}</dd>
    </dl>
  );
}

function LabelsForm({
  target,
  accessToken,
  onSaved,
  onUnavailable,
  onReload,
}: {
  target: MetadataTarget;
  accessToken: string;
  onSaved: (target: MetadataTarget) => void;
  onUnavailable: () => void;
  onReload: () => void;
}) {
  const id = useId();
  const [value, setValue] = useState(target.metadata.tags.join("\n"));
  const [validation, setValidation] = useState("");
  const attempt = useRef<IdempotencyAttempt | null>(null);
  const save = useMutation({
    mutationFn: (tags: string[]) => {
      const payload = { tags, expected_version: target.metadata.version };
      return retailRequest<MetadataTarget>(
        `/admin/object-metadata/${target.kind}/${encodeURIComponent(target.id)}`,
        accessToken,
        {
          method: "PATCH",
          headers: { "Idempotency-Key": managementAttempt(attempt, payload) },
          body: JSON.stringify(payload),
        },
      );
    },
    onSuccess: onSaved,
    onError: (error) => {
      if (error instanceof RetailRequestError && (error.status === 403 || error.status === 404)) onUnavailable();
    },
  });
  const conflict = managementConflict(save.error);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (save.isPending || conflict) return;
    const result = parseBusinessLabels(value);
    if (!result.ok) {
      setValidation(result.error);
      return;
    }
    setValidation("");
    save.mutate(result.tags);
  };
  return (
    <form className="space-y-4" onSubmit={submit}>
      <div className="space-y-2">
        <Label htmlFor={id}>Business labels</Label>
        <Textarea
          id={id}
          value={value}
          rows={6}
          maxLength={1000}
          disabled={save.isPending || conflict}
          onChange={(event) => setValue(event.target.value)}
          aria-describedby={`${id}-help`}
        />
        <p id={`${id}-help`} className="text-xs leading-relaxed text-muted-foreground">
          One label per line, up to 10. Start with a lowercase letter; use letters, numbers, and single hyphens. Leave
          empty to clear labels.
        </p>
      </div>
      {validation && (
        <p role="alert" className="text-sm text-destructive">
          {validation}
        </p>
      )}
      {save.error && (
        <Alert variant="error">
          <AlertTitle>{conflict ? "Labels changed" : "Unable to save labels"}</AlertTitle>
          <AlertDescription>
            {conflict
              ? "Another change reached this record first. Reload the label record to review its current version before saving again."
              : save.error.message}
          </AlertDescription>
        </Alert>
      )}
      <div className="flex flex-wrap justify-between gap-2">
        <Button type="button" variant="outline" onClick={onReload} disabled={save.isPending}>
          <RefreshCw />
          Reload label record
        </Button>
        <Button type="submit" disabled={save.isPending || conflict}>
          <Save />
          {save.isPending ? "Saving..." : "Save labels"}
        </Button>
      </div>
    </form>
  );
}

export default function BusinessLabelsEditor({
  selected,
  accessToken,
  userId,
  onUnavailable,
}: {
  selected: MetadataTarget;
  accessToken: string;
  userId: string | null;
  onUnavailable: () => void;
}) {
  const client = useQueryClient();
  const [reload, setReload] = useState(0);
  const detailKey = ["retail", "object-metadata", userId, "detail", selected.kind, selected.id];
  const targetOptions = {
    queryKey: detailKey,
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      retailRequest<MetadataTarget>(
        `/admin/object-metadata/${selected.kind}/${encodeURIComponent(selected.id)}`,
        accessToken,
        { signal },
      ),
    retry: false,
    refetchOnWindowFocus: false,
  };
  const target = useQuery(targetOptions);
  useEffect(() => {
    const error = target.error;
    if (error instanceof RetailRequestError && (error.status === 403 || error.status === 404)) onUnavailable();
  }, [target.error, onUnavailable]);
  const saved = (updated: MetadataTarget) => {
    client.setQueryData(detailKey, updated);
    void client.invalidateQueries({ queryKey: ["retail", "object-metadata", userId, "targets"] });
    void client.invalidateQueries({ queryKey: ["retail", "operation-events"] });
    toast.success("Business labels saved");
  };
  const refresh = async () => {
    const result = await target.refetch();
    if (!result.error) setReload((value) => value + 1);
  };
  return (
    <section className="space-y-5 rounded-xl border p-5" aria-label="Edit business labels">
      <div>
        <h2 className="break-words text-base font-semibold">{target.error ? "Object unavailable" : selected.name}</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Label changes use a separate version from the object’s own settings.
        </p>
      </div>
      {target.isPending && (
        <LoadingState title="Loading label record" description="Checking the current object and label version." />
      )}
      {target.error && (
        <>
          <ManagementError title="Unable to load label record" error={target.error} />
          <Button variant="outline" onClick={() => void refresh()} disabled={target.isFetching}>
            Retry label record
          </Button>
        </>
      )}
      {target.data && !target.error && (
        <>
          <p className="text-xs text-muted-foreground">
            Label version {target.data.metadata.version} · Times shown in Beijing time
          </p>
          <LabelsForm
            key={`${target.data.id}:${target.data.metadata.version}:${reload}`}
            target={target.data}
            accessToken={accessToken}
            onSaved={saved}
            onUnavailable={onUnavailable}
            onReload={() => void refresh()}
          />
          <div className="border-t pt-5">
            <LabelProvenance target={target.data} />
          </div>
        </>
      )}
    </section>
  );
}
