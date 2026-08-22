"use client";

import {
  useCallback,
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
} from "react";
import type { SnapshotAdapter } from "./replayCoordinator";
import type { RealtimeRuntime } from "./runtime";
import {
  createSSESubscriber,
  dispatchSSECallbackForScope,
  recoverSSESnapshotForScope,
  type SSECallbackInvocation,
  type SSEHandlers,
  type SSEStatus,
  type UseSSEOptions,
} from "./sseSubscription";
import {
  acquireRealtimeRuntime,
  releaseRealtimeRuntime,
  type RealtimeRuntimeLease,
} from "@/shared/realtime/runtimeRegistry";

export {
  dispatchSSEEventForScope,
  getSSEBackoffBaseDelay,
  isSSEScopeCurrent,
} from "./sseSubscription";
export type {
  SSEHandler,
  SSEHandlers,
  SSEStatus,
  UseSSEOptions,
} from "./sseSubscription";

export const REALTIME_TRANSPORT_MODE = "event-source-with-polling-fallback" as const;

function initialStatus(): SSEStatus {
  if (typeof document === "undefined") return "closed";
  return document.visibilityState === "hidden" ? "closed" : "connecting";
}

export function useSSE(
  channels: string[],
  handlers: SSEHandlers,
  options: UseSSEOptions = {},
): { status: SSEStatus; reconnect: () => void } {
  const [status, setStatus] = useState<SSEStatus>(initialStatus);
  const runtimeRef = useRef<RealtimeRuntime | null>(null);
  const channelKey = useMemo(() => [...channels].sort().join(","), [channels]);
  const eventKey = useMemo(
    () => Object.keys(handlers).sort().join(","),
    [handlers],
  );
  const scopeIdentity = options.scopeIdentity ?? channelKey;
  const hasRecoveryAdapter = typeof options.recoverSnapshot === "function";

  // React's Effect Event keeps committed callbacks fresh while the explicit
  // subscribed scope prevents an old runtime lease from delivering another
  // user's event during passive-effect cleanup.
  const emitScopedCallback = useEffectEvent(
    (subscribedScope: string, invocation: SSECallbackInvocation) =>
      dispatchSSECallbackForScope(
        subscribedScope,
        scopeIdentity,
        options.isScopeCurrent,
        {
          handlers,
          onOpen: options.onOpen,
          onError: options.onError,
          onControl: options.onControl,
          onProtocolIssue: options.onProtocolIssue,
          onAuthInvalidated: options.onAuthInvalidated,
          setStatus,
        },
        invocation,
      ),
  );
  const emitRecoverSnapshot = useEffectEvent(
    (
      subscribedScope: string,
      ...args: Parameters<SnapshotAdapter>
    ): ReturnType<SnapshotAdapter> =>
      recoverSSESnapshotForScope(
        subscribedScope,
        scopeIdentity,
        options.isScopeCurrent,
        options.recoverSnapshot,
        ...args,
      ),
  );

  useEffect(() => {
    const subscribedScope = scopeIdentity;
    if (!channelKey || typeof window === "undefined") {
      const timer = window.setTimeout(
        () =>
          emitScopedCallback(subscribedScope, {
            kind: "status",
            status: "closed",
          }),
        0,
      );
      return () => window.clearTimeout(timer);
    }

    const lease: RealtimeRuntimeLease = acquireRealtimeRuntime(
      channelKey.split(","),
    );
    const { runtime } = lease;
    runtimeRef.current = runtime;
    const unsubscribe = runtime.subscribe(
      createSSESubscriber({
        subscribedScope,
        eventNames: eventKey.split(",").filter(Boolean),
        emit: emitScopedCallback,
        recoverSnapshot: hasRecoveryAdapter ? emitRecoverSnapshot : undefined,
        hiddenCloseDelayMs: options.hiddenCloseDelayMs,
        maxRetryCount: options.maxRetryCount,
      }),
    );
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
      releaseRealtimeRuntime(lease);
    };
  }, [
    channelKey,
    eventKey,
    hasRecoveryAdapter,
    options.hiddenCloseDelayMs,
    options.maxRetryCount,
    options.onProtocolIssue,
    scopeIdentity,
  ]);

  const reconnect = useCallback(() => runtimeRef.current?.reconnect(), []);
  return { status, reconnect };
}
