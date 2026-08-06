import type { RealtimeControlEvent } from "./contracts";
import type {
  SnapshotAdapter,
  SnapshotExecutionContext,
} from "./replayCoordinator";
import type {
  RealtimeProtocolIssue,
  RealtimeStatus,
  RuntimeSubscriber,
} from "./runtime";

export type SSEHandler = (data: unknown, id: string) => void;
export interface SSEHandlers {
  [eventName: string]: SSEHandler;
}
export type SSEStatus = RealtimeStatus;

export interface UseSSEOptions {
  scopeIdentity?: string;
  isScopeCurrent?: (scopeIdentity: string) => boolean;
  onOpen?: (event: Event, context: SnapshotExecutionContext) => void;
  onError?: (event: Event) => void;
  onControl?: (event: RealtimeControlEvent) => void;
  onProtocolIssue?: (issue: RealtimeProtocolIssue) => void;
  onAuthInvalidated?: () => void;
  recoverSnapshot?: SnapshotAdapter;
  hiddenCloseDelayMs?: number;
  maxRetryCount?: number;
}

export type SSECallbackInvocation =
  | { kind: "event"; name: string; data: unknown; id: string }
  | {
      kind: "open";
      event: Event;
      context: SnapshotExecutionContext;
    }
  | { kind: "error"; event: Event }
  | { kind: "control"; event: RealtimeControlEvent }
  | { kind: "protocol-issue"; issue: RealtimeProtocolIssue }
  | { kind: "auth-invalidated" }
  | { kind: "status"; status: SSEStatus };

type SSECallbackBindings = {
  handlers: SSEHandlers;
  onOpen?: UseSSEOptions["onOpen"];
  onError?: UseSSEOptions["onError"];
  onControl?: UseSSEOptions["onControl"];
  onProtocolIssue?: UseSSEOptions["onProtocolIssue"];
  onAuthInvalidated?: UseSSEOptions["onAuthInvalidated"];
  setStatus: (status: SSEStatus) => void;
};

type ScopedCallbackEmitter = (
  subscribedScope: string,
  invocation: SSECallbackInvocation,
) => void;

type ScopedSnapshotEmitter = (
  subscribedScope: string,
  ...args: Parameters<SnapshotAdapter>
) => ReturnType<SnapshotAdapter>;

type SSESubscriberAdapterOptions = {
  subscribedScope: string;
  eventNames: readonly string[];
  emit: ScopedCallbackEmitter;
  recoverSnapshot?: ScopedSnapshotEmitter;
  hiddenCloseDelayMs?: number;
  maxRetryCount?: number;
};

export const DEFAULT_MAX_RETRY_COUNT = Number.POSITIVE_INFINITY;

export function getSSEBackoffBaseDelay(attempt: number): number {
  const boundedAttempt = Math.min(5, Math.max(0, Math.trunc(attempt)));
  return Math.min(30_000, 1000 * 2 ** boundedAttempt);
}

export function isSSEScopeCurrent(
  subscribedScope: string,
  currentScope: string,
  validateScope?: (scopeIdentity: string) => boolean,
): boolean {
  return (
    subscribedScope === currentScope &&
    (validateScope?.(subscribedScope) ?? true)
  );
}

function invokeCallback<Args extends unknown[]>(
  callback: ((...args: Args) => void) | undefined,
  ...args: Args
): boolean {
  if (!callback) return false;
  callback(...args);
  return true;
}

export function dispatchSSEEventForScope(
  subscribedScope: string,
  currentScope: string,
  validateScope: UseSSEOptions["isScopeCurrent"],
  handlers: SSEHandlers,
  name: string,
  data: unknown,
  id: string,
): boolean {
  if (!isSSEScopeCurrent(subscribedScope, currentScope, validateScope)) {
    return false;
  }
  return invokeCallback(handlers[name], data, id);
}

export function dispatchSSECallbackForScope(
  subscribedScope: string,
  currentScope: string,
  validateScope: UseSSEOptions["isScopeCurrent"],
  bindings: SSECallbackBindings,
  invocation: SSECallbackInvocation,
): boolean {
  if (!isSSEScopeCurrent(subscribedScope, currentScope, validateScope)) {
    return false;
  }
  switch (invocation.kind) {
    case "event":
      return invokeCallback(
        bindings.handlers[invocation.name],
        invocation.data,
        invocation.id,
      );
    case "open":
      return invokeCallback(
        bindings.onOpen,
        invocation.event,
        invocation.context,
      );
    case "error":
      return invokeCallback(bindings.onError, invocation.event);
    case "control":
      return invokeCallback(bindings.onControl, invocation.event);
    case "protocol-issue":
      return invokeCallback(bindings.onProtocolIssue, invocation.issue);
    case "auth-invalidated":
      return invokeCallback(bindings.onAuthInvalidated);
    case "status":
      bindings.setStatus(invocation.status);
      return true;
  }
}

function staleSSEScopeError(): Error {
  const error = new Error("stale SSE subscription scope");
  error.name = "AbortError";
  return error;
}

export function recoverSSESnapshotForScope(
  subscribedScope: string,
  currentScope: string,
  validateScope: UseSSEOptions["isScopeCurrent"],
  recoverSnapshot: SnapshotAdapter | undefined,
  ...args: Parameters<SnapshotAdapter>
): ReturnType<SnapshotAdapter> {
  if (!isSSEScopeCurrent(subscribedScope, currentScope, validateScope)) {
    return Promise.reject(staleSSEScopeError());
  }
  return recoverSnapshot
    ? recoverSnapshot(...args)
    : Promise.reject(new Error("snapshot adapter unavailable"));
}

export function createSSESubscriber({
  subscribedScope,
  eventNames,
  emit,
  recoverSnapshot,
  hiddenCloseDelayMs,
  maxRetryCount,
}: SSESubscriberAdapterOptions): RuntimeSubscriber {
  const snapshotAdapter = recoverSnapshot;
  return {
    handlers: Object.fromEntries(
      eventNames.map((name) => [
        name,
        (data: unknown, id: string) =>
          emit(subscribedScope, { kind: "event", name, data, id }),
      ]),
    ),
    onOpen: (event, context) =>
      emit(subscribedScope, { kind: "open", event, context }),
    onError: (event) =>
      emit(subscribedScope, { kind: "error", event }),
    onControl: (event) =>
      emit(subscribedScope, { kind: "control", event }),
    onProtocolIssue: (issue) =>
      emit(subscribedScope, { kind: "protocol-issue", issue }),
    onAuthInvalidated: () =>
      emit(subscribedScope, { kind: "auth-invalidated" }),
    recoverSnapshot: snapshotAdapter
      ? (...args) => snapshotAdapter(subscribedScope, ...args)
      : undefined,
    hiddenCloseDelayMs,
    maxRetryCount: maxRetryCount ?? DEFAULT_MAX_RETRY_COUNT,
    setStatus: (status) =>
      emit(subscribedScope, { kind: "status", status }),
  };
}
