"use client";

import {
  useCallback,
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
} from "react";
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
  const runtimeRef = useRef<RealtimeRuntime | null>(null);
  // 修复闭包陈旧：改用 useEffectEvent 取代「passive effect 里回写 ref」。
  // ref 要等 passive effect 冲刷才更新，这中间到达的 SSE 事件会命中上一轮渲染的
  // handlers/options；effect event 在 commit 阶段同步换实现，不存在这个窗口。
  const dispatchHandler = useEffectEvent(
    (name: string, data: unknown, id: string) => {
      handlers[name]?.(data, id);
    },
  );
  const emitOpen = useEffectEvent((event: Event) => options.onOpen?.(event));
  const emitError = useEffectEvent((event: Event) => options.onError?.(event));
  const emitControl = useEffectEvent((event: RealtimeControlEvent) =>
    options.onControl?.(event),
  );
  const emitRecoverSnapshot = useEffectEvent<SnapshotAdapter>(
    (scopes, reason, signal) => {
      const recover = options.recoverSnapshot;
      return recover
        ? recover(scopes, reason, signal)
        : Promise.reject(new Error("snapshot adapter unavailable"));
    },
  );

  const channelKey = useMemo(() => [...channels].sort().join(","), [channels]);
  const eventKey = useMemo(
    () => Object.keys(handlers).sort().join(","),
    [handlers],
  );
  const hasRecoveryAdapter = typeof options.recoverSnapshot === "function";

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
            (data: unknown, id: string) => dispatchHandler(name, data, id),
          ]),
      ),
      onOpen: emitOpen,
      onError: emitError,
      onControl: emitControl,
      recoverSnapshot: hasRecoveryAdapter ? emitRecoverSnapshot : undefined,
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
    hasRecoveryAdapter,
    options.hiddenCloseDelayMs,
    options.maxRetryCount,
  ]);

  const reconnect = useCallback(() => runtimeRef.current?.reconnect(), []);
  return { status, reconnect };
}
