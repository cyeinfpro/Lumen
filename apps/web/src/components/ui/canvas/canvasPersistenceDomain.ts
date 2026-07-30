import { useEffect } from "react";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import type { CanvasGraph } from "@/lib/canvas/types";
import { createBroadcastChannel } from "@/shared/realtime/browser";

export function isCanvasCoordinationBroadcast(value: unknown): value is
  | { type: "canvas.saved"; clientId: string; revision: number }
  | { type: "canvas.selection.changed"; revision?: number }
  | {
      type: "canvas.presence.ping";
      requestId: string;
      targetClientId: string;
    }
  | {
      type: "canvas.presence.pong";
      requestId: string;
      clientId: string;
    } {
  if (!value || typeof value !== "object") return false;
  const payload = value as Record<string, unknown>;
  if (payload.type === "canvas.selection.changed") return true;
  if (payload.type === "canvas.presence.ping") {
    return (
      typeof payload.requestId === "string" &&
      typeof payload.targetClientId === "string"
    );
  }
  if (payload.type === "canvas.presence.pong") {
    return (
      typeof payload.requestId === "string" &&
      typeof payload.clientId === "string"
    );
  }
  return (
    payload.type === "canvas.saved" &&
    typeof payload.clientId === "string" &&
    typeof payload.revision === "number" &&
    Number.isSafeInteger(payload.revision) &&
    payload.revision >= 0
  );
}

export function sameGraph(left: CanvasGraph, right: CanvasGraph): boolean {
  return (
    stableSerialize(comparableGraph(left)) ===
    stableSerialize(comparableGraph(right))
  );
}

export function comparableGraph(graph: CanvasGraph): CanvasGraph {
  return {
    ...graph,
    nodes: graph.nodes.map((node) => ({
      ...node,
      parent_group_id: node.parent_group_id ?? null,
      size: node.size ?? undefined,
      config: {
        ...CANVAS_NODE_SPECS[node.type].defaultConfig,
        ...node.config,
      },
      ui: {
        collapsed: node.ui?.collapsed === true,
        color_tag: node.ui?.color_tag ?? null,
        preset_id: node.ui?.preset_id ?? null,
      },
    })),
    edges: graph.edges.map((edge) => ({
      ...edge,
      pinned_execution_id: edge.pinned_execution_id ?? null,
      pinned_output_index: edge.pinned_output_index ?? null,
      role: edge.role ?? null,
      order: edge.order ?? null,
    })),
  };
}

export function stableSerialize(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableSerialize(item)).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableSerialize(record[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}

export const CANVAS_CLIENT_LEASE_TTL_MS = 120_000;
export const CANVAS_SUSPENDED_CLIENT_LEASE_TTL_MS = 30 * 60_000;
export const CANVAS_PRESENCE_PROBE_TIMEOUT_MS = 600;

export interface CanvasClientLease {
  tabId: string;
  updatedAt: number;
  state: "active" | "suspended";
}

export function useCanvasClientLease(
  canvasId: string,
  clientId: string,
  tabId: string,
) {
  useEffect(() => {
    const refresh = () =>
      writeCanvasClientLease(
        canvasId,
        clientId,
        tabId,
        document.visibilityState === "hidden" ? "suspended" : "active",
      );
    const clear = () => clearCanvasClientLease(canvasId, clientId, tabId);
    const handlePageHide = (event: PageTransitionEvent) => {
      if (event.persisted) {
        writeCanvasClientLease(canvasId, clientId, tabId, "suspended");
        return;
      }
      clear();
    };
    refresh();
    const heartbeat = window.setInterval(refresh, 15_000);
    window.addEventListener("pagehide", handlePageHide);
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(heartbeat);
      window.removeEventListener("pagehide", handlePageHide);
      window.removeEventListener("pageshow", refresh);
      document.removeEventListener("visibilitychange", refresh);
      clear();
    };
  }, [canvasId, clientId, tabId]);
}

export function browserClientId(canvasId: string, tabId: string): string {
  if (typeof window === "undefined") return `ssr-${canvasId}`;
  const key = `lumen:canvas-client:${canvasId}`;
  let existing: string | null = null;
  try {
    existing = window.sessionStorage.getItem(key);
  } catch {
    return randomId();
  }
  const lease = existing ? readCanvasClientLease(canvasId, existing) : null;
  const value =
    existing &&
    !(lease && lease.tabId !== tabId && canvasClientLeaseIsFresh(lease))
      ? existing
      : randomId();
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    return value;
  }
  writeCanvasClientLease(canvasId, value, tabId);
  return value;
}

export async function canvasClientLeaseIsActive(
  canvasId: string,
  clientId: string,
): Promise<boolean> {
  const lease = readCanvasClientLease(canvasId, clientId);
  if (lease && canvasClientLeaseIsFresh(lease)) {
    return true;
  }
  const presence = await probeCanvasClientPresence(canvasId, clientId);
  if (presence !== null) return presence;
  return lease === undefined;
}

export function probeCanvasClientPresence(
  canvasId: string,
  targetClientId: string,
): Promise<boolean | null> {
  if (typeof BroadcastChannel === "undefined") return Promise.resolve(null);
  let channel: BroadcastChannel;
  try {
    channel = createBroadcastChannel(`lumen:canvas:${canvasId}`);
  } catch {
    return Promise.resolve(null);
  }
  const requestId = randomId();
  return new Promise((resolve) => {
    let settled = false;
    const finish = (active: boolean) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      channel.close();
      resolve(active);
    };
    const timer = window.setTimeout(
      () => finish(false),
      CANVAS_PRESENCE_PROBE_TIMEOUT_MS,
    );
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const payload = event.data;
      if (
        isCanvasCoordinationBroadcast(payload) &&
        payload.type === "canvas.presence.pong" &&
        payload.requestId === requestId &&
        payload.clientId === targetClientId
      ) {
        finish(true);
      }
    };
    try {
      channel.postMessage({
        type: "canvas.presence.ping",
        requestId,
        targetClientId,
      });
    } catch {
      finish(false);
    }
  });
}

export function readCanvasClientLease(
  canvasId: string,
  clientId: string,
): CanvasClientLease | null | undefined {
  try {
    const raw = window.localStorage.getItem(
      canvasClientLeaseKey(canvasId, clientId),
    );
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<CanvasClientLease>;
    return typeof value.tabId === "string" &&
      typeof value.updatedAt === "number"
      ? {
          tabId: value.tabId,
          updatedAt: value.updatedAt,
          state: value.state === "suspended" ? "suspended" : "active",
        }
      : null;
  } catch {
    return undefined;
  }
}

export function writeCanvasClientLease(
  canvasId: string,
  clientId: string,
  tabId: string,
  state: CanvasClientLease["state"] = "active",
) {
  try {
    window.localStorage.setItem(
      canvasClientLeaseKey(canvasId, clientId),
      JSON.stringify({ tabId, updatedAt: Date.now(), state }),
    );
  } catch {
    // Draft persistence still works without cross-tab lease discovery.
  }
}

export function canvasClientLeaseIsFresh(
  lease: CanvasClientLease,
  now = Date.now(),
): boolean {
  const ttl =
    lease.state === "suspended"
      ? CANVAS_SUSPENDED_CLIENT_LEASE_TTL_MS
      : CANVAS_CLIENT_LEASE_TTL_MS;
  return now - lease.updatedAt < ttl;
}

export function clearCanvasClientLease(
  canvasId: string,
  clientId: string,
  tabId: string,
) {
  try {
    const key = canvasClientLeaseKey(canvasId, clientId);
    const lease = readCanvasClientLease(canvasId, clientId);
    if (lease?.tabId === tabId) window.localStorage.removeItem(key);
  } catch {
    // Ignore storage restrictions during teardown.
  }
}

export function canvasClientLeaseKey(canvasId: string, clientId: string): string {
  return `lumen:canvas-lease:${canvasId}:${clientId}`;
}

export function randomId(): string {
  return typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `canvas-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
