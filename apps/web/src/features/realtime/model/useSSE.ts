"use client";

import type {
  SSEHandlers,
  SSEStatus,
  UseSSEOptions,
} from "./sseSubscription";

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

export const REALTIME_TRANSPORT_MODE = "polling-only" as const;

const NOOP_RECONNECT = () => {};

export function useSSE(
  channels: string[],
  handlers: SSEHandlers,
  options: UseSSEOptions = {},
): { status: SSEStatus; reconnect: () => void } {
  void channels;
  void handlers;
  void options;
  return { status: "idle", reconnect: NOOP_RECONNECT };
}
