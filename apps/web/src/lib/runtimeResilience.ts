"use client";

import { useSyncExternalStore } from "react";

export type RealtimeRuntimeStatus =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "error";
export type SessionRuntimeStatus =
  | "unknown"
  | "public"
  | "revalidating"
  | "authenticated"
  | "degraded"
  | "unauthorized";

export type RuntimeResilienceSnapshot = {
  realtime: RealtimeRuntimeStatus;
  session: SessionRuntimeStatus;
};

type RecoveryKind = "realtime" | "session";
type RecoveryHandler = () => void;
export type SessionInvalidationReason =
  | "http_unauthorized"
  | "realtime_auth_invalidated"
  | "request_identity_mismatch";
export type SessionInvalidationHandler = (
  reason: SessionInvalidationReason,
) => void;

const SERVER_SNAPSHOT: RuntimeResilienceSnapshot = {
  realtime: "idle",
  session: "public",
};
let snapshot: RuntimeResilienceSnapshot = SERVER_SNAPSHOT;
const listeners = new Set<() => void>();
const recoveryHandlers: Record<RecoveryKind, Set<RecoveryHandler>> = {
  realtime: new Set(),
  session: new Set(),
};
const sessionInvalidationHandlers = new Set<SessionInvalidationHandler>();

function emitSnapshot(next: RuntimeResilienceSnapshot): void {
  if (
    next.realtime === snapshot.realtime &&
    next.session === snapshot.session
  ) {
    return;
  }
  snapshot = next;
  for (const listener of listeners) listener();
}

export function setRealtimeRuntimeStatus(
  realtime: RealtimeRuntimeStatus,
): void {
  emitSnapshot({ ...snapshot, realtime });
}

export function setSessionRuntimeStatus(session: SessionRuntimeStatus): void {
  emitSnapshot({ ...snapshot, session });
}

export function getRuntimeResilienceSnapshot(): RuntimeResilienceSnapshot {
  return snapshot;
}

export function useRuntimeResilience(): RuntimeResilienceSnapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getRuntimeResilienceSnapshot,
    () => SERVER_SNAPSHOT,
  );
}

export function registerRuntimeRecovery(
  kind: RecoveryKind,
  handler: RecoveryHandler,
): () => void {
  recoveryHandlers[kind].add(handler);
  return () => recoveryHandlers[kind].delete(handler);
}

export function requestRuntimeRecovery(kind?: RecoveryKind): void {
  const kinds: RecoveryKind[] = kind ? [kind] : ["session", "realtime"];
  for (const currentKind of kinds) {
    for (const handler of recoveryHandlers[currentKind]) {
      try {
        handler();
      } catch {
        // Each subsystem owns its recovery error reporting.
      }
    }
  }
}

export function registerSessionInvalidation(
  handler: SessionInvalidationHandler,
): () => void {
  sessionInvalidationHandlers.add(handler);
  return () => sessionInvalidationHandlers.delete(handler);
}

export function requestSessionInvalidation(
  reason: SessionInvalidationReason,
): void {
  setSessionRuntimeStatus("unauthorized");
  for (const handler of sessionInvalidationHandlers) {
    try {
      handler(reason);
    } catch {
      // Identity owns cleanup reporting; one callback must not block another.
    }
  }
}

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const PUBLIC_IDENTITY_WRITE_PATHS = new Set([
  "/auth/api-key/verify",
  "/auth/login",
  "/auth/password/reset-confirm",
  "/auth/password/reset-request",
  "/auth/signup",
  "/auth/signup/byok",
]);

export function normalizeApiPath(pathname: string): string {
  let path = pathname;
  try {
    if (/^https?:\/\//i.test(path)) path = new URL(path).pathname;
  } catch {
    return pathname;
  }
  path = path.split(/[?#]/, 1)[0] ?? path;
  if (!path.startsWith("/")) path = `/${path}`;
  if (path === "/api") return "/";
  return path.startsWith("/api/") ? path.slice(4) : path;
}

export function isHighRiskIdentityWrite(
  method: string,
  pathname: string,
): boolean {
  const normalizedMethod = method.toUpperCase();
  if (!WRITE_METHODS.has(normalizedMethod)) return false;
  const path = normalizeApiPath(pathname);
  return !PUBLIC_IDENTITY_WRITE_PATHS.has(path);
}
