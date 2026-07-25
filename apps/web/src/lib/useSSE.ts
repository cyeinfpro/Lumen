"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RealtimeControlEvent } from "./sse/contracts";
import type { SnapshotAdapter } from "./sse/replayCoordinator";
import {
  RealtimeRuntime,
  type RealtimeStatus,
} from "./sse/runtime";

export type SSEHandler = (data: unknown, id: string) => void;
export interface SSEHandlers {
  [eventName: string]: SSEHandler;
}
export type SSEStatus = RealtimeStatus;

export interface UseSSEOptions {
  onOpen?: (event: Event) => void;
  onError?: (event: Event) => void;
  onControl?: (event: RealtimeControlEvent) => void;
  recoverSnapshot?: SnapshotAdapter;
  hiddenCloseDelayMs?: number;
  maxRetryCount?: number;
}

const runtimes = new Map<string, RealtimeRuntime>();
const DEFAULT_MAX_RETRY_COUNT = Number.POSITIVE_INFINITY;

export function getSSEBackoffBaseDelay(attempt: number): number {
  const boundedAttempt = Math.min(5, Math.max(0, Math.trunc(attempt)));
  return Math.min(30_000, 1000 * 2 ** boundedAttempt);
}

function initialStatus(): SSEStatus {
  if (typeof document === "undefined") return "closed";
  return document.visibilityState === "hidden" ? "closed" : "connecting";
}

function acquireRuntime(channels: string[]): RealtimeRuntime {
  const key = [...channels].sort().join(",");
  let runtime = runtimes.get(key);
  if (!runtime) {
    runtime = new RealtimeRuntime({ channels: key.split(",").filter(Boolean) });
    runtimes.set(key, runtime);
  }
  return runtime;
}

export function useSSE(
  channels: string[],
  handlers: SSEHandlers,
  options: UseSSEOptions = {},
): { status: SSEStatus; reconnect: () => void } {
  const [status, setStatus] = useState<SSEStatus>(initialStatus);
  const handlersRef = useRef(handlers);
  const optionsRef = useRef(options);
  const runtimeRef = useRef<RealtimeRuntime | null>(null);
  useEffect(() => {
    handlersRef.current = handlers;
    optionsRef.current = options;
  });

  const channelKey = useMemo(() => [...channels].sort().join(","), [channels]);
  const eventKey = useMemo(
    () => Object.keys(handlers).sort().join(","),
    [handlers],
  );

  useEffect(() => {
    if (!channelKey || typeof window === "undefined") {
      const timer = setTimeout(() => setStatus("closed"), 0);
      return () => clearTimeout(timer);
    }
    const runtime = acquireRuntime(channelKey.split(","));
    runtimeRef.current = runtime;
    const unsubscribe = runtime.subscribe({
      handlers: Object.fromEntries(
        eventKey
          .split(",")
          .filter(Boolean)
          .map((name) => [
            name,
            (data: unknown, id: string) =>
              handlersRef.current[name]?.(data, id),
          ]),
      ),
      onOpen: (event) => optionsRef.current.onOpen?.(event),
      onError: (event) => optionsRef.current.onError?.(event),
      onControl: (event) => optionsRef.current.onControl?.(event),
      recoverSnapshot: (scopes, reason) => {
        const recover = optionsRef.current.recoverSnapshot;
        return recover
          ? recover(scopes, reason)
          : Promise.reject(new Error("snapshot adapter unavailable"));
      },
      hiddenCloseDelayMs: options.hiddenCloseDelayMs,
      maxRetryCount: options.maxRetryCount ?? DEFAULT_MAX_RETRY_COUNT,
      setStatus,
    });
    const onVisibility = () =>
      runtime.visibility(document.visibilityState === "visible");
    const onOnline = () => runtime.online(true);
    const onOffline = () => runtime.online(false);
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
      if (runtimeRef.current === runtime) runtimeRef.current = null;
      unsubscribe();
      if (!runtime.active()) runtimes.delete(channelKey);
    };
  }, [
    channelKey,
    eventKey,
    options.hiddenCloseDelayMs,
    options.maxRetryCount,
  ]);

  const reconnect = useCallback(() => runtimeRef.current?.reconnect(), []);
  return { status, reconnect };
}
