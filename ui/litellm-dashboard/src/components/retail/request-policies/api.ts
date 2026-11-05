"use client";

import { useEffect, useRef, useState } from "react";
import { RetailRequestError, retailRequest } from "../api";
import { managementAttempt, type IdempotencyAttempt } from "../management/ManagementShared";
import type { PolicySession } from "./types";

export const policyBase = "/admin/request-policies";
export function usePrivateResource<T>(path: string, session: PolicySession, revision = 0) {
  const requestKey = `${path}:${revision}`;
  const [state, setState] = useState<{ key: string; data?: T; error?: Error }>({ key: requestKey });
  if (state.key !== requestKey) setState({ key: requestKey });
  const onDenied = session.onDenied;
  useEffect(() => {
    const controller = new AbortController();
    void retailRequest<T>(`${policyBase}${path}`, session.token, { signal: controller.signal }).then(
      (data) => {
        if (!controller.signal.aborted) setState({ key: requestKey, data });
      },
      (cause: unknown) => {
        if (controller.signal.aborted) return;
        if (cause instanceof RetailRequestError && (cause.status === 401 || cause.status === 403)) {
          onDenied();
          return;
        }
        setState({ key: requestKey, error: cause instanceof Error ? cause : new Error("Unable to load this page.") });
      },
    );
    return () => controller.abort();
  }, [path, session.token, requestKey, onDenied]);
  return state.key === requestKey ? state : { key: requestKey };
}

export function usePolicyWrite(session: PolicySession, onSuccess: () => void) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const active = useRef<AbortController | null>(null);
  const attempt = useRef<IdempotencyAttempt | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const run = async (path: string, method: "POST" | "PUT" | "DELETE", body?: object) => {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setPending(true);
    setError(null);
    try {
      const options: RequestInit = {
        method,
        headers: { "Idempotency-Key": managementAttempt(attempt, { path, method, body }) },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      };
      await retailRequest<unknown>(`${policyBase}${path}`, session.token, options);
      if (!controller.signal.aborted) onSuccess();
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (cause instanceof RetailRequestError && (cause.status === 401 || cause.status === 403)) session.onDenied();
      else setError(cause instanceof Error ? cause : new Error("Unable to save these changes."));
    } finally {
      if (!controller.signal.aborted) {
        active.current = null;
        setPending(false);
      }
    }
  };
  return { pending, error, run };
}
