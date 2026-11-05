"use client";

import { useState, type FormEvent } from "react";
import { LoadingState } from "@/components/shared/LoadingState";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { ManagementError, managementConflict } from "../management/ManagementShared";
import { usePolicyWrite, usePrivateResource } from "./api";
import { PolicyField, PolicyName, PolicyWriteError, policyFieldId } from "./PolicyShared";
import type { PolicySession, PromptDetails, SavedPrompt } from "./types";

function PromptForm({
  session,
  prompt,
  onClose,
  onSaved,
}: {
  session: PolicySession;
  prompt: PromptDetails | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(prompt?.name ?? "");
  const [content, setContent] = useState(prompt?.content ?? "");
  const [enabled, setEnabled] = useState(prompt?.enabled ?? true);
  const write = usePolicyWrite(session, onSaved);
  const blocked = write.pending || managementConflict(write.error);
  const contentBytes = new TextEncoder().encode(content).length;
  const invalidContent = !content.trim() || contentBytes > 8000;
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (blocked || !name.trim() || invalidContent) return;
    const payload = {
      name: name.trim(),
      content,
      enabled,
      ...(prompt ? { expected_version: prompt.version } : {}),
    };
    void write.run(prompt ? `/prompts/${prompt.id}` : "/prompts", prompt ? "PUT" : "POST", payload);
  };
  return (
    <form
      aria-label={prompt ? "Edit saved prompt" : "Create saved prompt"}
      onSubmit={submit}
      className="space-y-5 rounded-lg border p-5"
    >
      <h2 className="text-lg font-semibold">{prompt ? "Edit saved prompt" : "Create saved prompt"}</h2>
      <PolicyWriteError error={write.error} />
      <PolicyName value={name} onChange={setName} disabled={blocked} />
      <PolicyField
        name="Prompt content"
        hint={`${contentBytes.toLocaleString()} / 8,000 UTF-8 bytes. Content is private to gateway administrators.`}
      >
        <Textarea
          id={policyFieldId("Prompt content")}
          value={content}
          onChange={(event) => setContent(event.target.value)}
          maxLength={8000}
          rows={8}
          required
          disabled={blocked}
          autoComplete="off"
        />
      </PolicyField>
      <label className="flex items-center gap-2 text-sm">
        <Checkbox checked={enabled} onCheckedChange={(value) => setEnabled(value === true)} disabled={blocked} />
        Prompt enabled
      </label>
      <p className="text-sm text-muted-foreground">
        Pausing a referenced prompt blocks requests that require it. Remove its policy reference before deleting it.
      </p>
      <div className="flex gap-2">
        <Button type="submit" disabled={blocked || !name.trim() || invalidContent}>
          {write.pending ? "Saving…" : "Save prompt"}
        </Button>
        <Button type="button" variant="outline" onClick={onClose} disabled={write.pending}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
function ExistingPrompt({
  session,
  prompt,
  onClose,
  onSaved,
}: {
  session: PolicySession;
  prompt: SavedPrompt;
  onClose: () => void;
  onSaved: () => void;
}) {
  const data = usePrivateResource<PromptDetails>(`/prompts/${prompt.id}`, session);
  if (data.error)
    return (
      <div>
        <ManagementError title="Unable to load prompt content" error={data.error} />
        <Button variant="outline" onClick={onClose}>
          Close editor
        </Button>
      </div>
    );
  if (!data.data)
    return (
      <div>
        <LoadingState title="Loading prompt content" description="Reading the selected prompt for editing." />
        <Button variant="outline" onClick={onClose}>
          Cancel
        </Button>
      </div>
    );
  return <PromptForm session={session} prompt={data.data} onClose={onClose} onSaved={onSaved} />;
}
export default function PromptEditor(props: {
  session: PolicySession;
  prompt: SavedPrompt | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  return props.prompt ? <ExistingPrompt {...props} prompt={props.prompt} /> : <PromptForm {...props} prompt={null} />;
}
