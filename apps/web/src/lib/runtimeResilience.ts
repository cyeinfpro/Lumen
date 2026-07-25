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

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function normalizeApiPath(pathname: string): string {
  if (pathname === "/api") return "/";
  return pathname.startsWith("/api/") ? pathname.slice(4) : pathname;
}

export function isHighRiskIdentityWrite(
  method: string,
  pathname: string,
): boolean {
  const normalizedMethod = method.toUpperCase();
  if (!WRITE_METHODS.has(normalizedMethod)) return false;
  const path = normalizeApiPath(pathname);
  if (path === "/me" && normalizedMethod === "DELETE") return true;
  if (path.startsWith("/me/sessions/") && normalizedMethod === "DELETE") {
    return true;
  }
  if (path.startsWith("/me/api-credentials/")) return true;
  if (path === "/me/redemptions" && normalizedMethod === "POST") return true;
  return path === "/admin" || path.startsWith("/admin/");
}
