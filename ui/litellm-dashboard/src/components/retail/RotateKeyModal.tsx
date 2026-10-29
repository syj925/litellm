"use client";

import { useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import CopyButton from "@/components/shared/CopyButton";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { retailRequest } from "./api";

export interface RotatedKey {
  key: string;
  token_id: string;
  key_name: string;
  rotated_at: string;
}

export default function RotateKeyModal({
  accessToken,
  tokenId,
  keyAlias,
  onClose,
  onRotated,
}: {
  accessToken: string | null;
  tokenId: string;
  keyAlias: string;
  onClose: () => void;
  onRotated: (key: Omit<RotatedKey, "key">) => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rotate = async () => {
    if (pending || !confirmed) return;
    setPending(true);
    setError(null);
    try {
      const result = await retailRequest<RotatedKey>("/keys/rotate", accessToken, {
        method: "POST",
        body: JSON.stringify({ key: tokenId }),
      });
      setSecret(result.key);
      onRotated({ token_id: result.token_id, key_name: result.key_name, rotated_at: result.rotated_at });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The API key could not be rotated.");
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !pending) onClose();
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{secret ? "API key rotated" : "Rotate API key"}</DialogTitle>
          <DialogDescription>
            {secret
              ? "Copy the new secret now. It will not be shown again after you close this window."
              : `Replace the secret for ${keyAlias || "this API key"}. Existing access rules and billing settings are preserved.`}
          </DialogDescription>
        </DialogHeader>
        {secret ? (
          <div className="space-y-4">
            <div>
              <Label htmlFor="rotated-api-secret">New API key</Label>
              <div className="mt-2 flex items-center gap-2">
                <Input
                  id="rotated-api-secret"
                  className="font-mono text-xs"
                  value={secret}
                  readOnly
                  autoComplete="off"
                />
                <CopyButton value={secret} label="Copy new API key" />
              </div>
            </div>
            <p className="text-sm text-muted-foreground">
              The old secret is no longer valid. Update the applications that use this key.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            <Alert variant="warning">
              <AlertTitle>The old secret stops working immediately</AlertTitle>
              <AlertDescription>
                Models, resource pools, budgets, spend, expiry, and rate limits keep their existing values. Rotation
                does not renew an expired key.
              </AlertDescription>
            </Alert>
            <div className="flex items-start gap-3 rounded-lg border p-4">
              <Checkbox
                id="confirm-key-rotation"
                checked={confirmed}
                onCheckedChange={(value) => setConfirmed(value === true)}
              />
              <Label htmlFor="confirm-key-rotation" className="text-sm leading-relaxed">
                I understand that applications using the old secret must be updated.
              </Label>
            </div>
          </div>
        )}
        {error && (
          <Alert variant="error">
            <AlertTitle>Key was not rotated</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={pending} onClick={onClose}>
            {secret ? "Done" : "Cancel"}
          </Button>
          {!secret && (
            <Button disabled={!confirmed || pending} onClick={() => void rotate()}>
              {pending ? "Rotating..." : "Rotate key"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
