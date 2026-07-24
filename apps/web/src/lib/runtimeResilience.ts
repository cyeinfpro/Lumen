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

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method;
  return typeof Request !== "undefined" && input instanceof Request
    ? input.method
    : "GET";
}

function requestPath(input: RequestInfo | URL): string | null {
  const raw =
    typeof input === "string"
      ? input
      : input instanceof URL
        ? input.href
        : input.url;
  try {
    const base =
      typeof window === "undefined" ? "http://localhost" : window.location.href;
    const url = new URL(raw, base);
    if (
      typeof window !== "undefined" &&
      url.origin === window.location.origin &&
      !url.pathname.startsWith("/api")
    ) {
      return null;
    }
    return url.pathname;
  } catch {
    return null;
  }
}

function blockedWriteResponse(session: SessionRuntimeStatus): Response {
  const unauthorized = session === "unauthorized";
  const message = unauthorized
    ? "登录状态已失效，请重新登录后再操作"
    : "正在确认登录状态，高风险操作已暂时切换为只读";
  return new Response(
    JSON.stringify({
      error: {
        code: unauthorized ? "unauthorized" : "identity_degraded",
        message,
      },
    }),
    {
      status: unauthorized ? 401 : 409,
      headers: { "content-type": "application/json" },
    },
  );
}

type FetchGuardState = {
  refs: number;
  original: typeof fetch;
  guarded: typeof fetch;
};

const FETCH_GUARD_KEY = "__lumenIdentityWriteGuard_v1__";
type GuardedGlobal = typeof globalThis & {
  [FETCH_GUARD_KEY]?: FetchGuardState;
};

export function installHighRiskIdentityWriteGuard(): () => void {
  if (typeof fetch === "undefined") return () => undefined;
  const guardedGlobal = globalThis as GuardedGlobal;
  let state = guardedGlobal[FETCH_GUARD_KEY];
  if (!state) {
    const original = globalThis.fetch.bind(globalThis);
    const guarded: typeof fetch = async (input, init) => {
      const path = requestPath(input);
      const method = requestMethod(input, init);
      const session = getRuntimeResilienceSnapshot().session;
      if (
        path &&
        session !== "authenticated" &&
        session !== "public" &&
        isHighRiskIdentityWrite(method, path)
      ) {
        return blockedWriteResponse(session);
      }
      return original(input, init);
    };
    state = { refs: 0, original, guarded };
    guardedGlobal[FETCH_GUARD_KEY] = state;
    globalThis.fetch = guarded;
  }
  state.refs += 1;

  let active = true;
  return () => {
    if (!active) return;
    active = false;
    const current = guardedGlobal[FETCH_GUARD_KEY];
    if (!current) return;
    current.refs = Math.max(0, current.refs - 1);
    if (current.refs > 0) return;
    if (globalThis.fetch === current.guarded) {
      globalThis.fetch = current.original;
    }
    delete guardedGlobal[FETCH_GUARD_KEY];
  };
}
